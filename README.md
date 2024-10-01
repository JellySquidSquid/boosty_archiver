# Boosty archiver

## Requirements

Python libraries:

* HTTP requests
	* `httpx`
	* `orjson`
* CLI
	* `typer`
	* `rich`
* Detecting image extension
	* `python-magic-bin; sys_platform == "win32"`
	* `python-magic; sys_platform != "win32"`
* Parsing HTML *(removed from requirements)*
	* `beautifulsoup4`
		* `html5lib`

## Basic usage

1. Save cookies from `boosty.to` into `cookies.txt` and put it near script.
2. Then copy your `Authorization: Bearer TOKEN` (only "TOKEN") into `token.txt`.
3. Run command:

```sh
.\boosty_archiver.py <NAME_OR_URL> [NAME2] [NAME3]
```
You either provide just name or whole URL to profile(s).

## Help command

```sh
.\boosty_archiver.py --help
```
```js
 Usage: boosty_archiver.py [OPTIONS] URLS...

 Archive all users by URLs / user names

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ *    urls      URLS...  URLs or user names from Boosty [required]                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --output_dir        -O      DIRECTORY  Specify different output root directory [default: (.)]                        │
│ --force-redownload  -F                 Do not skip files and redownload them again                                   │
│ --version           -V                 Shows archiver version in format "YYYY.MM.DD"                                 │
│ --help                                 Show this message and exit.                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Authorization ──────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --token    -T      TEXT  Specify "Authorization: Bearer __TOKEN__", otherwise load from "token.txt" [default: None]  │
│ --cookies  -C      PATH  Specify path to "cookies.txt" jar file [default: (cookies.txt)]                             │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Database ───────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --use-db     --no-use-db          Use sqlite3 DB file [default: no-use-db]                                           │
│ --db-path                   PATH  Specify custom DB path [default: None]                                             │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```
