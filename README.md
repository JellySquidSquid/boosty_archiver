# Boosty archiver

This small project added as a self-made solution to archive from Boosty.

It save post text, images and files in a format: `PostIntID_Title_Index_Filename.Extension`

Post text saved with similar format as `.txt`, used to save passwords.

Also all links that found in all posts exported in file `_post_links.txt` in a text table format `PostIntID	PostURL	URL` - separated by tabs, one URL per line. Used to export Google Drive, Mega and other links. Sorted by int id. It also keep old links in case your subscription is ended or old post moved to higher tier, this way if you not delete those links they will kept forever. They also would be kept even if they are updated in post.

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
.\boosty_archiver.py --use-db <NAME_OR_URL>
```
You either provide just name or whole URL to profile(s).
When DB is used, it will write downloads into sqlite3 file and check on future import if file was already downloaded. However it does not check file size this way if it was updated. When not using DB, it will check at least size from API with file size before attempting to redownload.

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

`token.txt` and `--token`/`-T` does not need to have `Bearer ` prefix

`cookies.txt` use standard spec: [**Netscape HTTP Cookie File**](http://curl.haxx.se/rfc/cookie_spec.html)
