"""Blue

Some folks like black but I prefer blue.
"""

import logging
import re
import sys

from configparser import ConfigParser
from importlib import machinery
from importlib.metadata import version as _get_version

# Black 1.0+ ships pre-compiled libraries with mypyc, which we can't
# monkeypatch like needed. In order to ensure that the original .py files get
# loaded instead, we create a custom FileFinder that excludes the
# ExtensionFileLoader, then use that as the file finder for Black's modules.
# However, we should perform this run time check to ensure we're not running
# in an environment we can't support.

if 'black' in sys.modules and sys.modules['black'].__file__.endswith(
    '.so'
):  # pragma: no cover
    raise RuntimeError(
        'A mypyc-compiled version of black has already been imported. '
        'This prevents blue from operating properly.'
    )


class NoMypycBlackFileFinder(machinery.FileFinder):
    def __init__(self, path: str, *loader_details) -> None:
        super().__init__(path, *loader_details)

        for hook in sys.path_hooks[1:]:
            try:
                self.original_finder = hook(path)
            except ImportError:
                continue
            else:
                break
        else:
            raise ImportError(
                'Failed to find original import finder'
            )  # pragma: no cover

    def find_spec(self, fullname, *args, **kw):
        if fullname == 'black' or fullname.startswith('black.'):
            return super().find_spec(fullname, *args, **kw)
        else:
            return self.original_finder.find_spec(fullname, *args, **kw)

    @classmethod
    def path_hook(cls):
        return super().path_hook(
            (machinery.SourceFileLoader, machinery.SOURCE_SUFFIXES),
            (machinery.SourcelessFileLoader, machinery.BYTECODE_SUFFIXES),
        )


sys.path_hooks.insert(0, NoMypycBlackFileFinder.path_hook())
sys.path_importer_cache.clear()


# These have to be imported after the import system hackery above, so we just
# ignore the E402 warning from flake8.
import black
import black.cache
import black.comments
import black.strings

from black import Leaf, Path, click, token
from black.cache import user_cache_dir
from black.comments import (
    LN,
    ProtoComment,
    make_comment,
    normalize_trailing_prefix,
)
from black.files import find_user_pyproject_toml, tomllib
from black.linegen import LineGenerator as BlackLineGenerator
from black.lines import Line
from black.nodes import (
    STANDALONE_COMMENT,
    is_docstring,
    is_multiline_string,
    make_simple_prefix,
)
from black.strings import (
    STRING_PREFIX_CHARS,
    get_string_prefix,
    normalize_string_prefix,
    normalize_unicode_escape_sequences,
    sub_twice,
)

from enum import Enum
from functools import lru_cache
from typing import Any, Dict, Iterator, List, Optional, Pattern

from click import Option
from click.decorators import version_option

LOG = logging.getLogger(__name__)

black_format_file_in_place = black.format_file_in_place
black_strings_fix_docstring = black.strings.fix_multiline_docstring
black_strings_normalize_string_quotes = black.strings.normalize_string_quotes

version = _get_version('blue')

# Try not to poison Black's cache directory.
black.cache.CACHE_DIR = Path(user_cache_dir('blue', version=version))


# Blue works by monkey patching black, so we don't have to duplicate
# everything, and we can take advantage of black's excellent implementation.
# We still have to monkey patch more than we want so eventually, these ought
# to be implemented by hooks in black that we can set.  Until then, there are
# essentially two modes of black operation we have to deal with.
#
# When black is formatting a single file, it's easy to monkey patch at an entry
# point for blue.  But when formatting multiple files, black uses some clever
# asynchronous parallelization which prevents us from monkey patching a few
# things in the blue entry point.  By way of code inspection and
# experimentation, we've found a convenient place to monkey patch a few things
# after the subprocesses have been spawned.  Define your monkey patch points
# here.


class Mode(Enum):
    asynchronous = 1
    synchronous = 2


BLUE_MONKEYPATCHES = [
    # Synchronous Monkees.
    (black, 'format_file_in_place', Mode.synchronous),
    (black, 'parse_pyproject_toml', Mode.synchronous),
    (black, 'LineGenerator', Mode.synchronous),
    (black.files, 'parse_pyproject_toml', Mode.synchronous),
    (black.linegen, 'normalize_string_quotes', Mode.synchronous),
    (black.strings, 'normalize_string_quotes', Mode.synchronous),
    (black.trans, 'normalize_string_quotes', Mode.synchronous),
    (black.comments, 'list_comments', Mode.synchronous),
    (black.linegen, 'list_comments', Mode.synchronous),
    # Asynchronous Monkees.
    (black, 'LineGenerator', Mode.asynchronous),
    (black.linegen, 'normalize_string_quotes', Mode.asynchronous),
    (black.strings, 'normalize_string_quotes', Mode.asynchronous),
    (black.trans, 'normalize_string_quotes', Mode.asynchronous),
    (black.comments, 'list_comments', Mode.asynchronous),
    (black.linegen, 'list_comments', Mode.asynchronous),
    (black.comments, 'generate_comments', Mode.asynchronous),
    (black.linegen, 'generate_comments', Mode.asynchronous),
]


def monkey_patch_black(mode: Mode) -> None:
    blue = sys.modules['blue']
    for module, function_name, monkey_mode in BLUE_MONKEYPATCHES:
        if monkey_mode is mode:
            setattr(module, function_name, getattr(blue, function_name))


# Because blue makes different choices than black, and all of this code is
# essentially ripped off from black, applying blue to it will change the
# formatting.  That will make diff'ing with black more difficult, so just turn
# off formatting for anything that comes from black.

# fmt: off

# Re(gex) does actually cache patterns internally but this still improves
# performance on a long list literal of strings by 5-9% since lru_cache's
# caching overhead is much lower.
@lru_cache(maxsize=64)
def _cached_compile(pattern: str) -> Pattern[str]:
    return re.compile(pattern)


def normalize_string_quotes(s: str) -> str:
    """Prefer *single* quotes but only if it doesn't cause more escaping.

    Adds or removes backslashes as appropriate. Doesn't parse and fix
    strings nested in f-strings.
    """
    value = s.lstrip(STRING_PREFIX_CHARS)
    if value[:3] == '"""':
        return s

    elif value[:3] == "'''":
        orig_quote = "'''"
        new_quote = '"""'
    elif value[0] == "'":
        orig_quote = "'"
        new_quote = '"'
    else:
        orig_quote = '"'
        new_quote = "'"
    first_quote_pos = s.find(orig_quote)
    if first_quote_pos == -1:
        return s  # There's an internal error

    prefix = s[:first_quote_pos]
    unescaped_new_quote = _cached_compile(rf'(([^\\]|^)(\\\\)*){new_quote}')
    escaped_new_quote = _cached_compile(rf'([^\\]|^)\\((?:\\\\)*){new_quote}')
    escaped_orig_quote = _cached_compile(rf'([^\\]|^)\\((?:\\\\)*){orig_quote}')
    body = s[first_quote_pos + len(orig_quote) : -len(orig_quote)]
    if 'r' in prefix.casefold():
        if unescaped_new_quote.search(body):
            # There's at least one unescaped new_quote in this raw string
            # so converting is impossible
            return s

        # Do not introduce or remove backslashes in raw strings
        new_body = body
    else:
        # remove unnecessary escapes
        new_body = sub_twice(escaped_new_quote, rf'\1\2{new_quote}', body)
        if body != new_body:
            # Consider the string without unnecessary escapes as the original
            body = new_body
            s = f'{prefix}{orig_quote}{body}{orig_quote}'
        new_body = sub_twice(escaped_orig_quote, rf'\1\2{orig_quote}', new_body)
        new_body = sub_twice(unescaped_new_quote, rf'\1\\{new_quote}', new_body)
    if 'f' in prefix.casefold():
        matches = re.findall(
            r"""
            (?:(?<!\{)|^)\{  # start of the string or a non-{ followed by a single {
                ([^{].*?)  # contents of the brackets except if begins with {{
            \}(?:(?!\})|$)  # A } followed by end of the string or a non-}
            """,
            new_body,
            re.VERBOSE,
        )
        for m in matches:
            if '\\' in str(m):
                # Do not introduce backslashes in interpolated expressions
                return s

    if new_quote == "'''" and new_body[-1:] == "'":
        # edge case:
        new_body = new_body[:-1] + "\\'"
    orig_escape_count = body.count('\\')
    new_escape_count = new_body.count('\\')
    if new_escape_count > orig_escape_count:
        return s  # Do not introduce more escaping

    if new_escape_count == orig_escape_count and orig_quote == "'":
        return s  # Prefer double quotes

    return f'{prefix}{new_quote}{new_body}{new_quote}'


def generate_comments(leaf: LN, *, mode: Mode) -> Iterator[Leaf]:
    total_consumed = 0
    for pc in list_comments(
        leaf.prefix, is_endmarker=leaf.type == token.ENDMARKER, mode=mode
    ):
        total_consumed = pc.consumed
        prefix = make_simple_prefix(pc.newlines, pc.form_feed) + pc.leading_whitespace
        yield Leaf(pc.type, pc.value, prefix=prefix)
    normalize_trailing_prefix(leaf, total_consumed)


# Like black's list_comments() but preserves whitespace leading up to the hash
# mark.  Because what we really need to do is restore the whitespace after the
# line.lstrip() statement, there really is no good way to more narrowly
# monkeypatch.  This would be a good hook to install.  See
# https://github.com/grantjenks/blue/issues/14
@lru_cache(maxsize=4096)
def list_comments(prefix: str, *, is_endmarker: bool, mode: Mode) -> List[ProtoComment]:
    """Return a list of :class:`ProtoComment` objects parsed from the given `prefix`."""
    result: List[ProtoComment] = []
    if not prefix or "#" not in prefix:
        return result

    consumed = 0
    nlines = 0
    ignored_lines = 0
    form_feed = False
    for index, full_line in enumerate(re.split('\r?\n|\r', prefix)):
        consumed += len(full_line) + 1  # adding the length of the split '\n'
        match = re.match(r'^(\s*)(\S.*|)$', full_line)
        assert match
        whitespace, line = match.groups()
        if not line:
            nlines += 1
            if '\f' in full_line:
                form_feed = True
        if not line.startswith('#'):
            # Escaped newlines outside of a comment are not really newlines at
            # all. We treat a single-line comment following an escaped newline
            # as a simple trailing comment.
            if line.endswith('\\'):
                ignored_lines += 1
            continue

        if index == ignored_lines and not is_endmarker:
            comment_type = token.COMMENT  # simple trailing comment
        else:
            comment_type = STANDALONE_COMMENT
        comment = make_comment(line, mode=mode)
        # Track the original whitespace for a line, adjusting down for the two
        # spaces black prepends
        whitespace = max(0, len(full_line) - len(line) - 2)
        result.append(
            ProtoComment(
                type=comment_type,
                value=comment,
                newlines=nlines,
                consumed=consumed,
                form_feed=form_feed,
                leading_whitespace=' ' * whitespace,
            )
        )
        form_feed = False
        nlines = 0
    return result


def parse_pyproject_toml(path_config: str) -> Dict[str, Any]:
    """Parse a pyproject toml file, pulling out relevant parts for Black

    If parsing fails, will raise a tomllib.TOMLDecodeError
    """
    with open(path_config, "rb") as f:
        pyproject_toml = tomllib.load(f)
    config = pyproject_toml.get("tool", {}).get("blue", {})
    return {k.replace("--", "").replace("-", "_"): v for k, v in config.items()}


def fix_docstring(docstring: str, prefix: str) -> str:
    new_docstring = black_strings_fix_docstring(docstring, prefix)
    # Needs special handling for module docstring case!
    if docstring.endswith('\n') and not new_docstring.endswith('\n'):
        new_docstring += '\n'
    return new_docstring


class LineGenerator(BlackLineGenerator):

    def visit_STRING(self, leaf: Leaf) -> Iterator[Line]:  # noqa: N802
        normalize_unicode_escape_sequences(leaf)

        if is_docstring(leaf) and not re.search(r"\\\s*\n", leaf.value):
            # We're ignoring docstrings with backslash newline escapes because changing
            # indentation of those changes the AST representation of the code.
            if self.mode.string_normalization:
                docstring = normalize_string_prefix(leaf.value)
                # We handle string normalization at the end of this method, but since
                # what we do right now acts differently depending on quote style (ex.
                # see padding logic below), there's a possibility for unstable
                # formatting. To avoid a situation where this function formats a
                # docstring differently on the second pass, normalize it early.
                docstring = normalize_string_quotes(docstring)
            else:
                docstring = leaf.value
            prefix = get_string_prefix(docstring)
            docstring = docstring[len(prefix) :]  # Remove the prefix
            quote_char = docstring[0]
            # A natural way to remove the outer quotes is to do:
            #   docstring = docstring.strip(quote_char)
            # but that breaks on """""x""" (which is '""x').
            # So we actually need to remove the first character and the next two
            # characters but only if they are the same as the first.
            quote_len = 1 if docstring[1] != quote_char else 3
            docstring = docstring[quote_len:-quote_len]
            docstring_started_empty = not docstring
            indent = " " * 4 * self.current_line.depth

            if is_multiline_string(leaf):
                docstring = black_strings_fix_docstring(docstring, indent)
            else:
                docstring = docstring.strip()

            has_trailing_backslash = False
            if docstring:
                # Add some padding if the docstring starts / ends with a quote mark.
                if docstring[0] == quote_char:
                    docstring = " " + docstring
                if docstring[-1] == quote_char:
                    docstring += " "
                if docstring[-1] == "\\":
                    backslash_count = len(docstring) - len(docstring.rstrip("\\"))
                    if backslash_count % 2:
                        # Odd number of tailing backslashes, add some padding to
                        # avoid escaping the closing string quote.
                        docstring += " "
                        has_trailing_backslash = True
            elif not docstring_started_empty:
                docstring = " "

            # Enforce triple quotes at this point.
            quote = '"""'

            # It's invalid to put closing single-character quotes on a new line.
            if quote_len == 3:
                # We need to find the length of the last line of the docstring
                # to find if we can add the closing quotes to the line without
                # exceeding the maximum line length.
                # If docstring is one line, we don't put the closing quotes on a
                # separate line because it looks ugly (#3320).
                lines = docstring.splitlines()
                last_line_length = len(lines[-1]) if docstring else 0

                # If adding closing quotes would cause the last line to exceed
                # the maximum line length, and the closing quote is not
                # prefixed by a newline then put a line break before
                # the closing quotes
                if (
                    len(lines) > 1
                    and last_line_length + quote_len > self.mode.line_length
                    and len(indent) + quote_len <= self.mode.line_length
                    and not has_trailing_backslash
                ):
                    if leaf.value[-1 - quote_len] == "\n":
                        leaf.value = prefix + quote + docstring + quote
                    else:
                        leaf.value = prefix + quote + docstring + "\n" + indent + quote
                else:
                    leaf.value = prefix + quote + docstring + quote
            else:
                leaf.value = prefix + quote + docstring + quote

        if self.mode.string_normalization and leaf.type == token.STRING:
            leaf.value = normalize_string_prefix(leaf.value)
            leaf.value = normalize_string_quotes(leaf.value)
        yield from self.visit_default(leaf)
# fmt: on


def format_file_in_place(*args, **kws):
    # This is a convenient place to monkey patch any function that must be
    # done after black's asynchronous invocation.
    monkey_patch_black(Mode.asynchronous)
    return black_format_file_in_place(*args, **kws)


def load_configs_from_file() -> Dict[str, Any]:
    """Parses supported config files using configparser"""
    supported_config_files = ('setup.cfg', 'tox.ini', '.blue')
    config_dict = {}
    pwd = Path.cwd()
    cfg = ConfigParser()

    config_file_found = False

    # search config files from pwd and its parents
    for directory in (pwd, *pwd.parents):
        filenames = [
            (directory / config_file) for config_file in supported_config_files
        ]
        files_read = cfg.read(filenames)

        # if config file was read, stop search
        if len(files_read) > 0:
            config_file_found = True
            break

    if not config_file_found:
        # config file not found yet
        # last try using top-level user configuration for black
        try:
            top_level_full_path = find_user_pyproject_toml()

            top_level_dir = top_level_full_path.parent

            filenames = [
                (top_level_dir / config_file)
                for config_file in supported_config_files
            ]

            cfg.read(filenames)
        except PermissionError:
            # ignore user level config directory if no access permission was given
            pass

    if cfg.has_section('blue'):
        config_dict.update(cfg.items('blue'))
    return config_dict


def read_configs(
    ctx: click.Context, param: click.Parameter, value: Optional[str]
) -> Optional[str]:
    """Read configs through the config param's callback hook."""
    # Use black's `read_pyproject_toml` for the default
    result = black.read_pyproject_toml(ctx, param, value)
    # parses setup.cfg, tox.ini, and .blue config files
    # The parsing looks both in the project and user directories.
    config = load_configs_from_file()
    # Merge the configs into Click's `default_map`.
    default_map: Dict[str, Any] = {}
    default_map.update(ctx.default_map or {})
    for key, value in config.items():
        key = key.replace('--', '').replace('-', '_')
        default_map[key] = value
    ctx.default_map = default_map
    return result


def _find_parameter(name: str):
    for parameter in black.main.params:
        if parameter.name == name:
            return parameter
    raise ValueError(f'Parameter name {name!r} not found!')


def main():
    monkey_patch_black(Mode.synchronous)
    # Reach in and monkey patch the Click options. This is tricky based on the
    # way Click works! This is highly fragile because the index into the Click
    # parameters is dependent on the decorator order for Black's main().
    # Change the default line length to 79 characters.
    line_length_param = _find_parameter('line_length')
    line_length_param.default = 79
    # Change the target version help doc to mention "Blue", not "Black".
    target_version_param = _find_parameter('target_version')
    target_version_param.help = target_version_param.help.replace(
        'Black', 'Blue'
    )
    # Change the config param callback to support setup.cfg, tox.ini, etc.
    config_param = _find_parameter('config')
    config_param.callback = read_configs
    # Change the version string.
    black.main.params = [
        p
        for p in black.main.params
        if not (isinstance(p, Option) and '--version' in p.opts)
    ]
    version_string = f'{version}, based on black {black.__version__}'
    version_option(version_string)(black.main)
    black.main()
