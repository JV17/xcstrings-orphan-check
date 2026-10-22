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

Usage
  check-orphan-strings.py [--policy manual|translated] [FILE ...]   staged changes (pre-commit hook)
  check-orphan-strings.py [--policy ...] --base origin/main          everything since the merge base (CI)

Exit status: 0 clean, 1 something to fix, 2 the tool itself failed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

SOURCE_EXTENSIONS = {".swift", ".m", ".mm"}
# Custom NSLocalizedString-style functions your code base uses, e.g. ["MyLocalizedString"].
EXTRA_LOCALIZATION_FUNCTIONS = []
EXTRACT_FLAGS = ["--SwiftUI", "--modern-localizable-strings", "--legacy-localizable-strings",
                 "--omit-empty-stringsdata"]

# The extractor writes "%arg" for interpolations; the compiler writes the real specifier ("%lld").
FORMAT_SPECIFIER = re.compile(
    r"%arg|%(?:\d+\$)?[-+ #0]*\d*(?:\.\d+)?(?:hh|h|ll|l|q|z|t|L)?[@dDiuUxXoOfeEgGcCsSpaAF]")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def fail(message):
    print(f"check-orphan-strings: {message}", file=sys.stderr)
    sys.exit(2)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def git(*args):
    result = run(["git", *args])
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def git_show(ref, path):
    """File content at ref ("" is the index), or None when the file does not exist there."""
    result = run(["git", "show", f"{ref}:{path}"])
    return result.stdout if result.returncode == 0 else None


def normalize(key):
    return FORMAT_SPECIFIER.sub("%arg", key)


def changed_lines(diff_args, path):
    """Line numbers in the new version of `path` that the diff adds or modifies."""
    lines = set()
    for raw in git("diff", "-U0", "--no-color", *diff_args, "--", path).splitlines():
        match = HUNK_HEADER.match(raw)
        if match:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            lines.update(range(start, start + count))
    return lines


def load_catalog(ref, path):
    content = git_show(ref, path)
    if content is None:
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        fail(f"{path} at {'index' if ref == '' else ref} is not valid JSON: {error}")


def catalog_tables(ref):
    """Table name -> set of normalized keys, across every tracked .xcstrings file."""
    tables = {}
    for path in git("ls-files", "-z", "--", "*.xcstrings").split("\0"):
        if path:
            table = os.path.splitext(os.path.basename(path))[0]
            keys = tables.setdefault(table, set())
            keys.update(normalize(k) for k in load_catalog(ref, path).get("strings", {}))
    return tables


def check_sources(paths, diff_args, tables, root):
    errors = []
    command = ["xcrun", "xcstringstool", "extract", *EXTRACT_FLAGS]
    for name in EXTRA_LOCALIZATION_FUNCTIONS:
        command += ["-s", name]
    for path in paths:
        # One output directory per file: same-named files in different folders would overwrite each other.
        with tempfile.TemporaryDirectory() as out_dir:
            result = run([*command, os.path.join(root, path), "-o", out_dir])
            if result.returncode != 0:
                fail(f"xcstringstool extract failed on {path} (Xcode 16 or newer is required):\n"
                     f"{result.stderr.strip()}")
            lines = None
            for name in os.listdir(out_dir):
                with open(os.path.join(out_dir, name)) as handle:
                    data = json.load(handle)
                for table, entries in data.get("tables", {}).items():
                    for entry in entries:
                        location = entry.get("location", {})
                        line, column = location.get("startingLine", 0), location.get("startingColumn", 0)
                        if lines is None:
                            lines = changed_lines(diff_args, path)
                        if line not in lines:
                            continue
                        key = entry["key"]
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
    errors = []
    for path in paths:
        new = load_catalog(head_ref, path)
        old = load_catalog(base_ref, path)
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
    parser.add_argument("files", nargs="*", help="limit the check to these files (Lefthook passes them)")
    args = parser.parse_args()

    root = git("rev-parse", "--show-toplevel").strip()
    explicit = [os.path.relpath(os.path.abspath(f), root) for f in args.files]
    os.chdir(root)

    if args.base:
        base = git("merge-base", args.base, "HEAD").strip()
        diff_args, head_ref, base_ref = [base, "HEAD"], "HEAD", base
    else:
        diff_args, head_ref, base_ref = ["--cached"], "", "HEAD"

    changed = explicit or [f for f in git("diff", "--name-only", "--diff-filter=AMR", *diff_args).splitlines() if f]
    sources = [f for f in changed if os.path.splitext(f)[1] in SOURCE_EXTENSIONS and os.path.exists(f)]
    catalogs = [f for f in changed if f.endswith(".xcstrings")]
    if not sources and not catalogs:
        return 0

    errors = []
    if sources:
        errors += check_sources(sources, diff_args, catalog_tables(head_ref), root)
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
    sys.exit(main())
