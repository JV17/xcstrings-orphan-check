# xcstrings-orphan-check

A pre-commit and CI check for iOS, macOS, watchOS and tvOS projects that use **String Catalogs**
(`.xcstrings`). It refuses a commit, and fails a pull request, when a change introduces a localizable
string that nobody added to the catalog deliberately. Only the lines a change adds or modifies and the
keys a change adds to a catalog are examined, so existing strings never block anyone.

No dependencies: git runs a three-line hook, the hook runs one Python script, and the script uses
`xcstringstool`, a tool that ships inside Xcode.

## The problem it solves

When a target has a String Catalog, every Xcode build extracts the localizable strings from the code
and adds any new key to the catalog. That is convenient, but it also means a developer who writes
`Text("Save changes")` and never opens the catalog ships a new key with no translations. The entry
sits in the catalog as if someone had reviewed it, translators never hear about it, and users see
English in every other language.

This check catches that at the two places it can still be fixed cheaply: on the developer's machine
when they commit, and on the pull request before it merges.

## How it works

Two checks run on every commit. Both look only at what the commit changes.

**1. Source check.** For every staged `.swift`, `.m` and `.mm` file, the script asks
`xcrun xcstringstool extract` for the localizable strings in that file and the line each one is on.
That tool is a lightweight parser, so nothing is compiled and it takes a fraction of a second per
file. It understands the same APIs the compiler extracts from: `Text`, `Button`, `Label`, `Toggle`,
`TextField`, `Picker`, `Section`, `.navigationTitle`, `.alert`, `.confirmationDialog`, `.searchable`,
`String(localized:)`, `LocalizedStringResource`, `AttributedString(localized:)`, `NSLocalizedString`
and friends, in Swift and Objective-C. Strings on lines the commit added or changed must already
exist in the catalog of their table. Anything else is reported with file, line and column.

**2. Catalog check.** For every staged `.xcstrings` file, the script compares the keys with the
previous commit. A key that is new must have been added on purpose. The catalog format makes that
easy to tell: a key that an Xcode build extracted from code is written with no `extractionState`,
while a key added by hand in the catalog editor carries `"extractionState" : "manual"`.

If either check finds something, the script prints the findings and exits with status 1, and git
aborts the commit. Fix the strings, stage the catalog, commit again.

## Requirements

- Xcode 16 or newer on every developer Mac and CI runner. The `extract` subcommand of
  `xcstringstool` shipped with Xcode 16. The check has been exercised with Xcode 27.
- The project uses String Catalogs. Projects still on `.strings` files have nothing to compare
  against.
- git, and the `python3` that comes with Xcode's command line tools (3.9 or newer).

## Install in a repository

1. Copy the `scripts/` and `.githooks/` folders into the root of the repository and commit them.
2. Tell git where the hook lives. This is a per-clone setting, so each developer runs it once:

   ```sh
   git config core.hooksPath .githooks
   ```

   Add that line to the project's setup instructions or bootstrap script so new clones get it.

That is the whole installation. Git runs `.githooks/pre-commit` before every commit from then on.
Commits from the terminal and from git clients that call git, such as Fork, Tower or SourceTree,
all run it. Xcode's own commit sheet is reported to run pre-commit hooks as well; make your first
test commit from wherever your team usually commits to confirm it on your setup.

### Try it in two minutes

```sh
git checkout -b orphan-check-test
# add   Text("Orphan test string")   to any SwiftUI view, then
git add -A
git commit -m "test"
```

The commit is refused and the output names the file and line:

```
Sources/SettingsView.swift:42:13: error: "Orphan test string" is not in Localizable.xcstrings; add it to the catalog first

1 new localizable string(s) are not in a String Catalog on purpose. Add them in the catalog editor (the + button marks them Manual), stage the catalog, retry.
```

Open the String Catalog in Xcode, press the **+** button, enter `Orphan test string` and its
translations, then stage the catalog and commit again. The commit goes through.

## When a commit is blocked

Each finding is one line in the `file:line:column: error: message` format, the same format Xcode
uses, so it is easy to read and easy to jump to.

| Message | Meaning | Fix |
| --- | --- | --- |
| `"…" is not in Localizable.xcstrings; add it to the catalog first` | The string is used in code but the catalog of its table has no such key. | Add the key in the catalog editor with **+**, fill in the translations, stage the catalog. |
| `new key "…" was extracted from code by an Xcode build, not added deliberately` | A build wrote the key into the catalog and the change is about to commit it that way. | In the catalog editor select the key, switch it to manual management from the context menu, add the translations. Or delete the key, add it with **+**. |
| `new key "…" has no translation for de, fr` | Only with `--policy translated`: the key lacks a value in languages the catalog already has. | Fill in the missing languages. |
| `"…" needs a Other.xcstrings catalog and none is tracked` | Code uses a table for which no catalog exists in the repository. | Create the catalog or fix the table name. |

A string that must not be translated, such as a product name, can be marked **Don't translate** in
the catalog editor. The check accepts those.

`git commit --no-verify` skips every pre-commit hook. That is why the same script also runs on the
pull request, which is the enforcing step.

## Run it by hand

```sh
python3 scripts/check-orphan-strings.py                       # the staged changes
python3 scripts/check-orphan-strings.py --base origin/main    # everything on this branch
python3 scripts/check-orphan-strings.py Sources/SettingsView.swift   # only the listed files
```

Exit status is 0 when clean, 1 when something must be fixed, and 2 when the tool itself could not
run, for example because the selected Xcode is too old.

## Continuous integration

Run the script on every pull request with `--base` set to the target branch. It compares the pull
request with the merge base, so only the branch's own changes are judged. The checkout needs full
history so the merge base can be found.

GitHub Actions example:

```yaml
name: Orphan strings
on: pull_request
jobs:
  orphan-strings:
    runs-on: [self-hosted, macOS]   # any macOS runner with Xcode 16 or newer selected
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: python3 scripts/check-orphan-strings.py --base origin/${{ github.base_ref }}
```

Any other CI works the same way: check out with history on a Mac that has Xcode, then run the
command above with the target branch. Make the job a required status check so the rule cannot be
bypassed.

## Options

**Policy.** What counts as "added deliberately" for a new catalog key.

| Flag | Rule | Fits a team that |
| --- | --- | --- |
| `--policy manual` (default) | The key is marked Manual in the catalog. | adds strings to the catalog first, then uses them in code |
| `--policy translated` | The key has a value for every language the catalog already contains. | lets Xcode extract strings and translates before merging |

To change the policy for the hook, append the flag to the last line of `.githooks/pre-commit`.
For CI, append it to the command.

**Custom localization helpers.** If the code base has its own functions with the shape of
`NSLocalizedString`, list their names in `EXTRA_LOCALIZATION_FUNCTIONS` at the top of the script
and they are extracted too.

**Several catalogs.** Catalogs are matched to code by table name: a string with no explicit table
belongs to `Localizable.xcstrings`, `String(localized:table:)` and `NSLocalizedStringFromTable`
select another file. Every tracked catalog with that file name counts, so a monorepo with several
apps works, with the caveat below.

## What it does not catch

- **Your own APIs that take a `LocalizedStringKey` or `LocalizedStringResource`.** A custom view
  such as `Banner(title: "…")` or a custom initializer such as `Text(localized: "…")` is
  localized by the compiler exactly like `Text("…")`, so a build does add the key to the catalog.
  The extractor does not know your types, so the source check does not see these. The catalog
  check still catches them once the developer has built, because the build writes the key into
  the catalog before the commit. A commit made without building slips through for these APIs
  only. If the code base relies on such wrappers, add a build to the CI job and compare the
  catalog after it, or ask for the wrappers to be added to the script's extraction patterns.
- **Strings in unstaged edits.** The hook judges what is being committed. An unstaged edit is checked
  when it is staged.
- **A key that exists in another target's catalog of the same name.** With two
  `Localizable.xcstrings` files in one repository, a key present in either satisfies the source
  check.
- **Interpolated strings** are matched loosely: `Count: %lld` in the catalog matches
  `Text("Count: \(count)")` in code whatever the type of `count`.

## Questions

**Does it slow down commits?** A commit with a handful of changed Swift files takes well under a
second longer. Nothing is compiled.

**Does it change my files?** No. It reads the staged content and the catalogs and prints findings.

**Will it complain about the hundreds of existing untranslated strings?** No. It only looks at the
lines a commit adds or changes and at the keys a commit adds to a catalog. Existing debt is yours
to clean up at your own pace.

**Do I need a hook manager such as Lefthook or pre-commit?** No. Those tools help when a team runs
several hooks. This one is a single file that git runs on its own.

**How do I remove it?** `git config --unset core.hooksPath` in the clone, and delete the two folders
from the repository.

## Example project

`example/OrphanDemo` is a small SwiftUI iOS app with a String Catalog in English and Spanish. Every
string it uses is a Manual entry with both translations, and string extraction is on, exactly as in a
project created from the Xcode template. Use it to watch the hook work before touching a real
repository:

1. In your clone of this repository run `git config core.hooksPath .githooks` once.
2. Open `example/OrphanDemo/OrphanDemo.xcodeproj`, add `Text("Orphan test string")` to
   `ContentView`, and build. Xcode adds the key to `Localizable.xcstrings` on its own.
3. Run `git add -A` and `git commit -m "demo"`. The commit is refused because the new catalog key is
   not marked Manual. Commit without building instead and the finding names the line in
   `ContentView.swift`.
4. In the catalog editor switch the key to manual management, or delete it and add it again with
   **+**, give it a Spanish value, stage the catalog and commit again. The commit goes through.

The project also builds from the command line without opening a simulator:

```sh
xcodebuild -project example/OrphanDemo/OrphanDemo.xcodeproj -target OrphanDemo \
  -sdk iphonesimulator CODE_SIGNING_ALLOWED=NO build
```

## Tests

`tests/run_tests.py` exercises the check end to end. Each test creates a throwaway git repository
with a small catalog and view, applies one scenario, runs the check and asserts the exit status and
the messages. It needs only git and Xcode.

```sh
python3 tests/run_tests.py          # all scenarios, about fifteen seconds
python3 tests/run_tests.py hook     # only the tests whose name contains "hook"
```

The scenarios cover new strings in Swift and Objective-C, existing keys on changed lines,
interpolation placeholders, keys written by a build, Manual and Don't-translate keys, both policies,
other tables and new catalogs, unstaged and deleted files, the explicit file list, several catalogs
with the same name, the CI mode on a branch, the hook refusing a commit and `--no-verify`, the
custom-API gap, the example project's own strings, and the exit status 2 cases. A GitHub Actions
workflow runs the suite on macOS runners.

## Repository layout

```
.githooks/pre-commit              the hook: three lines that call the script
scripts/check-orphan-strings.py   the check, one file, standard library only
tests/run_tests.py                end-to-end tests, standard library only
example/OrphanDemo/               SwiftUI demo app with an English and Spanish catalog
.github/workflows/tests.yml       runs the tests on macOS runners
README.md
```
