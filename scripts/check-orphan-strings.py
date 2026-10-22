#!/usr/bin/env python3
"""Block new localizable strings that were not deliberately added to a String Catalog.

Only what the commit (or the pull request) adds is checked, so existing debt never blocks anyone.

  1. Source check   A localizable string on an added or changed line of a .swift/.m/.mm file
                    must already exist in the .xcstrings catalog of its table.
                    Strings are found with `xcrun xcstringstool extract` (Xcode 16+); nothing is compiled.
  2. Catalog check  A key that is new in a staged .xcstrings file must have been added deliberately:
                      --policy manual      extractionState is "manual" (what the "+" button in the
                                           catalog editor writes; a build-extracted key has no state)
                      --policy translated  every language already present in the catalog has a value

The content being committed is what gets checked, not the working tree, so a partially staged file
is judged exactly as it will be committed. A moved or renamed file adds nothing. Each tool is
started once per check, not once per file, so a commit with many files stays fast.

Usage
  check-orphan-strings.py [--policy manual|translated] [FILE ...]   staged changes (pre-commit hook)
  check-orphan-strings.py [--policy ...] --base origin/main          everything since the merge base (CI)

Exit status: 0 clean, 1 something to fix, 2 the tool itself failed.
"""
import argparse
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

SOURCE_EXTENSIONS = (".swift", ".m", ".mm")
SOURCE_PATHSPECS = ["*.swift", "*.m", "*.mm"]
# Custom NSLocalizedString-style functions your code base uses, e.g. ["MyLocalizedString"].
EXTRA_LOCALIZATION_FUNCTIONS = []
EXTRACT_FLAGS = ["--SwiftUI", "--modern-localizable-strings", "--legacy-localizable-strings",
                 "--omit-empty-stringsdata"]
FILES_PER_EXTRACT_CALL = 1000          # keeps the command line far below the system limit

# The extractor writes "%arg" for interpolations; the compiler writes the real specifier ("%lld").
FORMAT_SPECIFIER = re.compile(
    r"%arg|%(?:\d+\$)?[-+ #0]*\d*(?:\.\d+)?(?:hh|h|ll|l|q|z|t|L)?[@dDiuUxXoOfeEgGcCsSpaAF]")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
MULTILINE_OPENING = re.compile(r'(#*)"""\s*$')


def fail(message):
    print(f"check-orphan-strings: {message}", file=sys.stderr)
    sys.exit(2)


_git = None


def git_binary():
    """On macOS /usr/bin/git is a shim that looks up Xcode on every call; resolving it once
    halves the cost of each git command. Any other git on the PATH is used as it is."""
    global _git
    if _git is None:
        _git = "git"
        if shutil.which("git") == "/usr/bin/git":
            found = subprocess.run(["xcrun", "--find", "git"], capture_output=True, text=True)
            if found.returncode == 0 and found.stdout.strip():
                _git = found.stdout.strip()
    return _git


def git(*args, stdin=None):
    result = subprocess.run([git_binary(), *args], input=stdin, capture_output=True)
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def text(data):
    return data.decode("utf-8", "surrogateescape")


def normalize(key):
    return FORMAT_SPECIFIER.sub("%arg", key)


def unquote(path):
    """Undo git's C-style quoting of names that contain quotes, backslashes or control characters."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        raw = codecs.escape_decode(path[1:-1].encode("utf-8", "surrogateescape"))[0]
        return raw.decode("utf-8", "surrogateescape")
    return path


def changed_files(diff_args):
    out = text(git("diff", "--name-only", "-z", "-M", "--diff-filter=AMR", *diff_args))
    return [path for path in out.split("\0") if path]


def added_lines(diff_args):
    """{path: line numbers the diff adds or modifies} for every source file, from one `git diff`."""
    out = text(git("-c", "core.quotePath=false", "diff", "-U0", "-M", "--no-color", "--no-ext-diff", "--no-textconv",
                   "--src-prefix=a/", "--dst-prefix=b/", *diff_args, "--", *SOURCE_PATHSPECS))
    lines, current, in_header = {}, None, False
    for raw in out.split("\n"):
        if raw.startswith("diff --git "):
            current, in_header = None, True
        elif in_header and raw.startswith("+++ "):
            name = unquote(raw[4:].rstrip("\t"))        # git appends a tab when the name has a space
            current = None if name == "/dev/null" else name[2:]
        elif raw.startswith("@@"):
            in_header = False
            match = HUNK_HEADER.match(raw)
            if match and current is not None:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                lines.setdefault(current, set()).update(range(start, start + count))
    return lines


_blobs = {}


def read_blobs(specs):
    """{(ref, path): content, or None when absent}. Ref "" is the index. Whatever is not cached yet
    is read with one `git cat-file --batch`."""
    specs = list(dict.fromkeys(specs))
    wanted = [spec for spec in specs if spec not in _blobs]
    if wanted:
        if any("\n" in path for _, path in wanted):
            fail("file names that contain a line break are not supported")
        request = "".join(f"{ref}:{path}\n" for ref, path in wanted).encode("utf-8", "surrogateescape")
        out, position = git("cat-file", "--batch", stdin=request), 0
        for spec in wanted:
            end = out.index(b"\n", position)
            header, position = out[position:end], end + 1
            if header.endswith((b" missing", b" ambiguous")):
                _blobs[spec] = None
                continue
            size = int(header.rsplit(b" ", 1)[1])
            _blobs[spec] = out[position:position + size]
            position += size + 1
    return {spec: _blobs[spec] for spec in specs}


def same_path(path):
    """Comparable form of a path: the extractor reports names decomposed (NFD), git composed (NFC)."""
    return unicodedata.normalize("NFC", os.path.realpath(path))


def extract(contents):
    """[(path, table, key, line, column)] for every localizable string in the given file contents."""
    command = ["xcrun", "xcstringstool", "extract", *EXTRACT_FLAGS]
    for name in EXTRA_LOCALIZATION_FUNCTIONS:
        command += ["-s", name]
    found = []
    with tempfile.TemporaryDirectory() as scratch:
        by_source, calls = {}, []
        for path, content in contents.items():
            copy = os.path.join(scratch, "src", path)
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            with open(copy, "wb") as handle:
                handle.write(content)
            by_source[same_path(copy)] = path
            # One call covers many files. Files in the same call get distinct names, so their
            # outputs can never overwrite each other, whatever the Xcode version.
            name = os.path.basename(path)
            call = next((c for c in calls if name not in c and len(c) < FILES_PER_EXTRACT_CALL), None)
            if call is None:
                call = {}
                calls.append(call)
            call[name] = copy
        for index, call in enumerate(calls):
            out_dir = os.path.join(scratch, f"out{index}")
            os.mkdir(out_dir)
            result = subprocess.run([*command, *call.values(), "-o", out_dir], capture_output=True, text=True)
            if result.returncode != 0:
                fail(f"xcstringstool extract failed (Xcode 16 or newer is required):\n{result.stderr.strip()}")
            for name in os.listdir(out_dir):
                with open(os.path.join(out_dir, name), "rb") as handle:
                    data = json.loads(handle.read())
                path = by_source.get(same_path(data.get("source", "")))
                if path is None:
                    fail(f"cannot match extractor output {name} to a source file")
                for table, entries in data.get("tables", {}).items():
                    for entry in entries:
                        location = entry.get("location", {})
                        found.append((path, table, entry["key"],
                                      location.get("startingLine", 0), location.get("startingColumn", 0)))
    return found


def literal_lines(lines, line):
    """Lines spanned by the literal that starts on `line`: one, or several for a multi-line literal."""
    if 0 < line <= len(lines):
        opening = MULTILINE_OPENING.search(lines[line - 1])
        if opening:
            closing = '"""' + opening.group(1)
            for index in range(line, len(lines)):
                if lines[index].lstrip().startswith(closing):
                    return range(line, index + 2)
    return range(line, line + 1)


_catalogs = {}


def load_catalogs(specs):
    """{(ref, path): parsed catalog, {} when the file is absent at ref}. Each version is read once."""
    for spec, content in read_blobs([spec for spec in specs if spec not in _catalogs]).items():
        ref, path = spec
        try:
            catalog = {} if content is None else json.loads(content)
        except ValueError as error:
            fail(f"{path} at {'index' if ref == '' else ref} is not valid JSON: {error}")
        if not isinstance(catalog, dict) or not isinstance(catalog.get("strings", {}), dict):
            fail(f"{path} at {'index' if ref == '' else ref} is not a String Catalog")
        _catalogs[spec] = catalog
    return {spec: _catalogs[spec] for spec in specs}


def table_name(path):
    return os.path.splitext(os.path.basename(path))[0]


def tracked_catalogs():
    return [path for path in text(git("ls-files", "-z", "--", "*.xcstrings")).split("\0") if path]


def catalog_tables(ref, needed, tracked):
    """Table name -> normalized keys, parsing only the tracked catalogs of the needed tables."""
    paths = [path for path in tracked if table_name(path) in needed]
    tables = {}
    for (_, path), catalog in load_catalogs([(ref, path) for path in paths]).items():
        tables.setdefault(table_name(path), set()).update(normalize(key) for key in catalog.get("strings", {}))
    return tables


def check_sources(paths, added, ref, tracked):
    blobs = read_blobs([(ref, path) for path in paths])
    contents = {path: blobs[(ref, path)] for path in paths if blobs[(ref, path)] is not None}
    order = {path: index for index, path in enumerate(paths)}
    split, hits = {}, []
    for path, table, key, line, column in extract(contents) if contents else []:
        if path not in split:
            split[path] = text(contents[path]).split("\n")
        if not added[path].isdisjoint(literal_lines(split[path], line)):
            hits.append((order[path], line, column, path, table, key))
    if not hits:
        return []
    tables = catalog_tables(ref, {hit[4] for hit in hits}, tracked)
    errors = []
    for _, line, column, path, table, key in sorted(hits):
        if table not in tables:
            errors.append(f'{path}:{line}:{column}: error: "{key}" needs a '
                          f'{table}.xcstrings catalog and none is tracked')
        elif normalize(key) not in tables[table]:
            errors.append(f'{path}:{line}:{column}: error: "{key}" is not in '
                          f'{table}.xcstrings; add it to the catalog first')
    return errors


def missing_languages(entry, languages):
    localizations = entry.get("localizations") or {}
    return sorted(lang for lang in languages if not localizations.get(lang))


def check_catalogs(paths, head_ref, base_ref, policy):
    loaded = load_catalogs([(ref, path) for path in paths for ref in (head_ref, base_ref)])
    errors = []
    for path in paths:
        new, old = loaded[(head_ref, path)], loaded[(base_ref, path)]
        languages = {lang
                     for catalog in (old, new)
                     for entry in catalog.get("strings", {}).values()
                     for lang in (entry.get("localizations") or {})}
        languages.discard(new.get("sourceLanguage"))
        for key, entry in new.get("strings", {}).items():
            if key in old.get("strings", {}) or entry.get("shouldTranslate") is False:
                continue
            if policy == "manual":
                if entry.get("extractionState") != "manual":
                    errors.append(f'{path}: error: new key "{key}" was extracted from code by an Xcode '
                                  f'build, not added deliberately (it is not marked Manual)')
            else:
                missing = missing_languages(entry, languages)
                if missing:
                    errors.append(f'{path}: error: new key "{key}" has no translation for '
                                  f'{", ".join(missing)}')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policy", choices=["manual", "translated"], default="manual")
    parser.add_argument("--base", metavar="REF",
                        help="compare against the merge base with REF instead of the staged changes")
    parser.add_argument("files", nargs="*", help="limit the check to these files")
    args = parser.parse_args()

    root = text(git("rev-parse", "--show-toplevel")).strip()
    explicit = {os.path.relpath(os.path.abspath(f), root) for f in args.files}
    os.chdir(root)

    if args.base:
        base = text(git("merge-base", args.base, "HEAD")).strip()
        diff_args, head_ref, base_ref = [base, "HEAD"], "HEAD", base
    else:
        diff_args, head_ref, base_ref = ["--cached"], "", "HEAD"

    changed = [path for path in changed_files(diff_args) if not explicit or path in explicit]
    sources = [path for path in changed if path.endswith(SOURCE_EXTENSIONS)]
    catalogs = [path for path in changed if path.endswith(".xcstrings")]
    if not sources and not catalogs:
        return 0

    added = added_lines(diff_args) if sources else {}
    sources = [path for path in sources if added.get(path)]          # a pure deletion adds no strings
    tracked = tracked_catalogs() if sources else []
    # One git process reads every file version the checks may need.
    read_blobs([(head_ref, path) for path in sources + tracked]
               + [(ref, path) for path in catalogs for ref in (head_ref, base_ref)])

    errors = []
    if sources:
        errors += check_sources(sources, added, head_ref, tracked)
    if catalogs:
        errors += check_catalogs(catalogs, head_ref, base_ref, args.policy)

    for line in errors:
        print(line)
    if errors:
        print(f"\n{len(errors)} new localizable string(s) are not in a String Catalog on purpose. "
              f"Add them in the catalog editor (the + button marks them Manual), stage the catalog, retry.")
        return 1
    print("check-orphan-strings: OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                       # a crash must not look like a finding (exit 1)
        import traceback
        traceback.print_exc()
        sys.exit(2)
