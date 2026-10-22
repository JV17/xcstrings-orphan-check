#!/usr/bin/env python3
"""Self-contained tests for scripts/check-orphan-strings.py.

Each test builds a throwaway git repository with a small String Catalog and a SwiftUI view,
applies one scenario, runs the check and asserts the exit status and the messages.
Needs git and an Xcode with `xcstringstool extract` (Xcode 16 or newer). Standard library only.

    python3 tests/run_tests.py            # run everything
    python3 tests/run_tests.py hook       # run the tests whose name contains "hook"
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-orphan-strings.py"
HOOK = ROOT / ".githooks" / "pre-commit"

# Reuse the script's own key normalization ("%lld" and the extractor's "%arg" both become "%arg").
_spec = importlib.util.spec_from_file_location("check_orphan_strings", SCRIPT)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
normalize = _module.normalize
CATALOG = "App/Localizable.xcstrings"
VIEW = "App/ContentView.swift"


def unit(value, state="translated"):
    return {"stringUnit": {"state": state, "value": value}}


BASE_CATALOG = {
    "sourceLanguage": "en",
    "version": "1.0",
    "strings": {
        "existing.manual": {"extractionState": "manual",
                            "localizations": {"en": unit("Existing"), "es": unit("Existente")}},
        "Legacy orphan": {},
        "Count: %lld": {"localizations": {"es": unit("Cuenta: %lld")}},
        "Items: %lld": {"localizations": {"es": unit("Elementos: %lld")}},
        "ACME": {"shouldTranslate": False},
    },
}

BASE_VIEW = r'''import SwiftUI

struct ContentView: View {
    let n = 1
    var body: some View {
        VStack {
            Text(String(localized: "existing.manual"))
            Text("Legacy orphan")
            Text("Count: \(n)")
        }
    }
}
'''

ORPHAN_VIEW = r'''struct Extra: View {
    var body: some View {
        Button("Brand new orphan") {}
    }
}
'''
ORPHAN_LINE = BASE_VIEW.count("\n") + 3   # the Button line once ORPHAN_VIEW is appended


class Repo:
    def __init__(self, directory):
        self.path = Path(directory)
        (self.path / "scripts").mkdir()
        shutil.copy(SCRIPT, self.path / "scripts" / SCRIPT.name)
        self.git("-c", "init.defaultBranch=main", "init", "-q", ".")
        self.git("config", "user.email", "tests@example.com")
        self.git("config", "user.name", "Tests")
        self.save_catalog(BASE_CATALOG)
        self.write(VIEW, BASE_VIEW)
        self.commit("base")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.path, check=True,
                              capture_output=True, text=True).stdout

    def write(self, relative, text):
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def append(self, relative, text):
        with open(self.path / relative, "a") as handle:
            handle.write(text)

    def catalog(self, relative=CATALOG):
        return json.loads((self.path / relative).read_text())

    def save_catalog(self, data, relative=CATALOG):
        self.write(relative, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def add_key(self, key, entry, relative=CATALOG):
        data = self.catalog(relative) if (self.path / relative).exists() else \
            {"sourceLanguage": "en", "version": "1.0", "strings": {}}
        data["strings"][key] = entry
        self.save_catalog(data, relative)

    def stage(self):
        self.git("add", "-A")

    def commit(self, message):
        self.stage()
        self.git("commit", "-q", "-m", message)

    def check(self, *args):
        result = subprocess.run([sys.executable, "scripts/check-orphan-strings.py", *args],
                                cwd=self.path, capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr


def manual(key, es):
    return {"extractionState": "manual", "localizations": {"en": unit(key), "es": unit(es)}}


def expect(condition, detail):
    if not condition:
        raise AssertionError(detail)


def expect_blocked(result, *fragments):
    code, output = result
    expect(code == 1, f"expected exit 1, got {code}:\n{output}")
    for fragment in fragments:
        expect(fragment in output, f"expected {fragment!r} in:\n{output}")
    return output


def expect_clean(result):
    code, output = result
    expect(code == 0, f"expected exit 0, got {code}:\n{output}")
    return output


TESTS = []


def test(function):
    TESTS.append(function)
    return function


@test
def clean_tree_passes(repo):
    expect_clean(repo.check())


@test
def new_string_in_code_is_blocked_with_its_line(repo):
    repo.append(VIEW, ORPHAN_VIEW)
    repo.stage()
    output = expect_blocked(repo.check(), '"Brand new orphan" is not in Localizable.xcstrings',
                            "1 new localizable string(s)")
    match = re.search(r"App/ContentView\.swift:(\d+):(\d+): error:", output)
    expect(match and int(match.group(1)) == ORPHAN_LINE,
           f"expected line {ORPHAN_LINE} in:\n{output}")


@test
def existing_keys_on_new_lines_are_not_flagged(repo):
    repo.append(VIEW, r'''struct Reuse: View {
    let n = 2
    var body: some View {
        Text("Legacy orphan")
        Text(String(localized: "existing.manual"))
        Text("ACME")
        Text("Count: \(3)")
        Text("Items: \(n)")
    }
}
''')
    repo.stage()
    expect_clean(repo.check())


@test
def interpolation_matches_a_catalog_key_with_a_real_specifier(repo):
    repo.append(VIEW, r'''struct Counter: View {
    let n = 2
    var body: some View { Text("Items: \(n)") }
}
''')
    repo.stage()
    expect_clean(repo.check())


@test
def new_swift_file_is_checked(repo):
    repo.write("App/NewView.swift", 'import SwiftUI\n'
               'struct NewView: View { var body: some View { Label("Save changes", systemImage: "tray") } }\n')
    repo.stage()
    expect_blocked(repo.check(), 'App/NewView.swift:2:', '"Save changes" is not in Localizable.xcstrings')


@test
def objective_c_file_is_checked(repo):
    repo.write("App/Legacy.m", '#import <Foundation/Foundation.h>\n'
               'void f(void) { NSString *s = NSLocalizedString(@"ObjC string", @"comment"); }\n')
    repo.stage()
    expect_blocked(repo.check(), 'App/Legacy.m:2:', '"ObjC string" is not in Localizable.xcstrings')


@test
def key_written_by_a_build_is_blocked_by_the_catalog_check(repo):
    repo.append(VIEW, ORPHAN_VIEW)
    repo.add_key("Brand new orphan", {})          # exactly what an Xcode build writes
    repo.stage()
    output = expect_blocked(repo.check(), 'App/Localizable.xcstrings: error: new key "Brand new orphan"',
                            "not marked Manual")
    expect("ContentView.swift:" not in output, f"source check should pass once the key exists:\n{output}")


@test
def key_extracted_with_default_value_is_blocked(repo):
    repo.append(VIEW, 'struct Login: View { var body: some View { Text(String(localized: "login.title", defaultValue: "Sign in")) } }\n')
    repo.add_key("login.title", {"extractionState": "extracted_with_value",
                                 "localizations": {"en": unit("Sign in", "new")}})
    repo.stage()
    expect_blocked(repo.check(), 'new key "login.title"')


@test
def manual_key_with_translations_passes(repo):
    repo.append(VIEW, ORPHAN_VIEW)
    repo.add_key("Brand new orphan", manual("Brand new orphan", "Nuevo"))
    repo.stage()
    expect_clean(repo.check())


@test
def dont_translate_key_passes(repo):
    repo.append(VIEW, 'struct Brand: View { var body: some View { Text("Brand name") } }\n')
    repo.add_key("Brand name", {"shouldTranslate": False})
    repo.stage()
    expect_clean(repo.check())


@test
def policy_translated_requires_every_language(repo):
    repo.append(VIEW, ORPHAN_VIEW)
    repo.add_key("Brand new orphan", {"localizations": {"en": unit("Brand new orphan")}})
    repo.stage()
    expect_blocked(repo.check("--policy", "translated"), 'new key "Brand new orphan" has no translation for es')
    repo.add_key("Brand new orphan", {"localizations": {"en": unit("Brand new orphan"), "es": unit("Nuevo")}})
    repo.stage()
    expect_clean(repo.check("--policy", "translated"))
    expect_blocked(repo.check("--policy", "manual"), "not marked Manual")


@test
def other_table_without_a_catalog_is_reported(repo):
    repo.append(VIEW, 'struct T: View { var body: some View { Text(String(localized: "Other table key", table: "Other")) } }\n')
    repo.stage()
    expect_blocked(repo.check(), '"Other table key" needs a Other.xcstrings catalog')


@test
def other_table_with_a_new_catalog_passes(repo):
    repo.append(VIEW, 'struct T: View { var body: some View { Text(String(localized: "Other table key", table: "Other")) } }\n')
    repo.add_key("Other table key", manual("Other table key", "Clave"), relative="App/Other.xcstrings")
    repo.stage()
    expect_clean(repo.check())


@test
def keys_in_a_brand_new_catalog_must_be_deliberate(repo):
    repo.add_key("Fresh key", {}, relative="App/Other.xcstrings")
    repo.stage()
    expect_blocked(repo.check(), 'App/Other.xcstrings: error: new key "Fresh key"')


@test
def unstaged_changes_are_ignored(repo):
    repo.append(VIEW, ORPHAN_VIEW)
    expect_clean(repo.check())


@test
def deleted_file_is_ignored(repo):
    (repo.path / VIEW).unlink()
    repo.stage()
    expect_clean(repo.check())


@test
def explicit_file_list_limits_the_check(repo):
    repo.append(VIEW, ORPHAN_VIEW)
    repo.write("App/NewView.swift", 'import SwiftUI\nstruct NewView: View { var body: some View { Text("Second orphan") } }\n')
    repo.stage()
    output = expect_blocked(repo.check("App/NewView.swift"), '"Second orphan"')
    expect("Brand new orphan" not in output, f"only the listed file should be checked:\n{output}")


@test
def a_second_catalog_with_the_same_table_name_counts(repo):
    repo.add_key("Widget only", manual("Widget only", "Solo widget"), relative="Widget/Localizable.xcstrings")
    repo.commit("add widget catalog")
    repo.append(VIEW, 'struct W: View { var body: some View { Text("Widget only") } }\n')
    repo.stage()
    expect_clean(repo.check())


@test
def base_mode_catches_a_commit_made_without_building(repo):
    repo.git("checkout", "-q", "-b", "feature")
    repo.append(VIEW, ORPHAN_VIEW)
    repo.commit("add view without building")
    expect_blocked(repo.check("--base", "main"), '"Brand new orphan" is not in Localizable.xcstrings')


@test
def base_mode_passes_a_clean_branch(repo):
    repo.git("checkout", "-q", "-b", "feature")
    repo.append(VIEW, ORPHAN_VIEW)
    repo.add_key("Brand new orphan", manual("Brand new orphan", "Nuevo"))
    repo.commit("add view and key")
    expect_clean(repo.check("--base", "main"))


@test
def hook_blocks_the_commit_and_no_verify_bypasses_it(repo):
    (repo.path / ".githooks").mkdir()
    shutil.copy(HOOK, repo.path / ".githooks" / "pre-commit")
    repo.git("config", "core.hooksPath", ".githooks")
    repo.append(VIEW, ORPHAN_VIEW)
    repo.stage()
    before = repo.git("rev-list", "--count", "HEAD").strip()
    attempt = subprocess.run(["git", "commit", "-q", "-m", "orphan"], cwd=repo.path,
                             capture_output=True, text=True)
    expect(attempt.returncode != 0, "the hook should refuse the commit")
    expect("Brand new orphan" in attempt.stdout + attempt.stderr, "the hook output should name the string")
    expect(repo.git("rev-list", "--count", "HEAD").strip() == before, "no commit should have been created")
    repo.git("commit", "-q", "--no-verify", "-m", "bypassed")
    expect(int(repo.git("rev-list", "--count", "HEAD")) == int(before) + 1, "--no-verify should commit")


@test
def custom_localizable_api_is_a_documented_gap_of_the_source_check(repo):
    repo.append(VIEW, r'''extension Text {
    init(localized key: LocalizedStringKey) { self.init(key) }
}
struct Wrapped: View { var body: some View { Text(localized: "Wrapped key") } }
''')
    repo.stage()
    expect_clean(repo.check())                      # the parser cannot see custom APIs
    repo.add_key("Wrapped key", {})                 # but a build writes the key, and that is caught
    repo.stage()
    expect_blocked(repo.check(), 'new key "Wrapped key"')


@test
def outside_a_git_repository_exits_2(repo):
    with tempfile.TemporaryDirectory() as plain:
        shutil.copy(SCRIPT, Path(plain) / SCRIPT.name)
        result = subprocess.run([sys.executable, SCRIPT.name], cwd=plain, capture_output=True, text=True)
        expect(result.returncode == 2, f"expected exit 2, got {result.returncode}: {result.stderr}")


@test
def invalid_catalog_json_exits_2(repo):
    repo.write(CATALOG, "{ this is not json\n")
    repo.stage()
    code, output = repo.check()
    expect(code == 2 and "not valid JSON" in output, f"expected exit 2 with a JSON message, got {code}:\n{output}")

@test
def example_project_strings_are_all_in_its_catalog(repo):
    example = ROOT / "example" / "OrphanDemo" / "OrphanDemo"
    catalog = json.loads((example / "Localizable.xcstrings").read_text())["strings"]
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(["xcrun", "xcstringstool", "extract", "--SwiftUI", "--modern-localizable-strings",
                        "--legacy-localizable-strings", *map(str, example.glob("*.swift")), "-o", out],
                       check=True, capture_output=True)
        keys = [entry["key"] for file in Path(out).glob("*.stringsdata")
                for entries in json.loads(file.read_text())["tables"].values() for entry in entries]
    expect(keys, "the example should contain localizable strings")
    manual_keys = {normalize(k) for k, v in catalog.items() if v.get("extractionState") == "manual"}
    for key in keys:
        expect(normalize(key) in manual_keys, f"{key!r} must be a Manual entry in the example catalog")


def main():
    probe = subprocess.run(["xcrun", "xcstringstool", "extract", "--help"], capture_output=True, text=True)
    if probe.returncode != 0:
        print("xcrun xcstringstool extract is not available; Xcode 16 or newer must be selected.")
        return 2
    xcode = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True).stdout.splitlines()
    print(f"Testing {SCRIPT.relative_to(ROOT)} with {xcode[0] if xcode else 'unknown Xcode'}\n")
    selected = [t for t in TESTS if not sys.argv[1:] or any(word in t.__name__ for word in sys.argv[1:])]
    failures = 0
    for case in selected:
        with tempfile.TemporaryDirectory() as directory:
            try:
                case(Repo(directory))
                print(f"PASS  {case.__name__}")
            except AssertionError as error:
                failures += 1
                print(f"FAIL  {case.__name__}\n      {str(error).replace(chr(10), chr(10) + '      ')}")
            except Exception as error:   # noqa: BLE001 - report and keep going
                failures += 1
                print(f"ERROR {case.__name__}: {error!r}")
    print(f"\n{len(selected) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
