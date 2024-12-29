"""
Boosty downloader

Run with --help argument to see arguments.
"""

from typing import Annotated, Literal, NotRequired, TypedDict
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from functools import partial
from http.cookiejar import MozillaCookieJar
from pathlib import Path
import re
import sqlite3
from time import sleep

# from bs4 import BeautifulSoup  # + html5lib
import httpx
import magic  # python-magic (Linux) or python-magic-bin (Windows)
import orjson
import rich
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, MofNCompleteColumn, Progress, TaskID, TimeElapsedColumn, TimeRemainingColumn, TransferSpeedColumn
from rich.table import Table
import rich.traceback
import typer

__version__ = "2024.10.01"

# ? API
API_PREFIX = "https://api.boosty.to/v1/"
API_POSTS_PER_PAGE = 100
API_GET_CURRENT_USER = f"{API_PREFIX}user/current"
API_GET_SUBSCRIPTIONS = f"{API_PREFIX}user/subscriptions?limit=30&with_follow=false"  # include / exclude followers
API_GET_USER = f"{API_PREFIX}blog/{{user}}"
API_GET_POSTS_FIRST = f"{API_PREFIX}blog/{{user}}/post/?limit={API_POSTS_PER_PAGE}&comments_limit=0&reply_limit=0&is_only_allowed=false"
API_GET_POSTS = f"{API_PREFIX}blog/{{user}}/post/?limit={API_POSTS_PER_PAGE}&offset={{offset}}&comments_limit=0&reply_limit=0&is_only_allowed=false"

# ? Image extension detection with magic
# NOTE: avif and jxl is not supported yet by magic, they detected as "application/octet-stream"
MIME_TO_EXTENSION: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/x-ms-bmp": "bmp",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "video/webm": "webm",
    "video/mp4": "mp4",
    "video/x-matroska": "mkv",
    "video/x-msvideo": "avi",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/x-wav": "wav",
    "audio/x-flac": "flac",
    "audio/x-m4a": "m4a",
    "video/ogg": "ogv",
    "audio/ogg": "ogg",  # ? .opus also guessed as audio/ogg by magic
    "audio/x-hx-aac-adts": "aac",
}

# ? DB
DB_PATH = Path(__file__).parent / "archive.db"
CREATE_TABLE = "CREATE TABLE IF NOT EXISTS archive (entry PRIMARY KEY) WITHOUT ROWID;"
CHECK_ENTRY = "SELECT EXISTS(SELECT true FROM archive WHERE entry = '{entry}');"
INSERT_ENTRY = "INSERT INTO archive (entry) VALUES ('{entry}') ON CONFLICT DO NOTHING;"

USER_CHECK: re.Pattern[str] = re.compile(r"^(?P<domain>https?:\/\/(?:www\.)?boosty\.to\/)?(?P<user>[\w\-\.]+)", flags=re.IGNORECASE)

FILENAME_REPLACEMENTS: Sequence[tuple[str, str]] = [
    ("\\", "⧹"),
    ("/", "⧸"),
    (":", "："),
    ("*", "✩"),
    ("?", "？"),
    ('"', "＂"),
    ("<", "⧼"),
    (">", "⧽"),
    ("|", "｜"),
]

FILENAME_CONTROLS: re.Pattern[str] = re.compile(r"[\000-\031]")

VIDEO_QUALITY_ORDER: Sequence[str] = ("ultra_hd", "quad_hd", "full_hd", "high", "medium", "tiny", "low", "lowest")

# ? Models


class Social(TypedDict):
    url: str
    type: Literal["facebook", "twitter", "website"]


class Subscription(TypedDict):
    custumPrice: int
    id: int
    isApplePayed: bool
    levelId: int
    offTime: int  # unix timestamp
    onTime: int  # unix timestamp
    period: int


class InitialStateBlogBlogData(TypedDict):
    """
    Only nessesary fields are typed, others are omitted

    "signedQuery" not present when authentication is not successful
    """

    blogUrl: str
    coverUrl: str
    signedQuery: NotRequired[str]
    description: Sequence[dict]
    socialLinks: Sequence[Social]
    subscription: Subscription
    isSubscribed: bool
    subscriptionKind: Literal["paid", "free", "none"]
    title: str


class InitialStateBlogBlog(TypedDict):
    data: InitialStateBlogBlogData
    meta: dict


class InitialStateBlog(TypedDict):
    """Only nessesary fields are typed, others are omitted"""

    blog: InitialStateBlogBlog


class InitialState(TypedDict):
    """Only nessesary fields are typed, others are omitted"""

    blog: InitialStateBlog


class CurrentUser(TypedDict):
    """Only nessesary fields are typed, others are omitted"""

    id: int
    blogUrl: str  # same as id, but str
    email: str
    name: str
    hasAvatar: bool
    avatarUrl: str | None
    timezone: int  # 100 -> UTC+1
    defaultCurrency: Literal["USD", "RUB"]
    locale: Literal["en", "ru"]
    hasSubscriptions: bool
    hasFreeSubscriptions: bool
    hasPaidSubscriptions: bool


class User(TypedDict):
    """
    Only nessesary fields are typed, others are omitted

    "signedQuery" not present when authentication is not successful
    """

    isSubscribed: bool
    subscription: dict | None
    subscriptionKind: Literal["paid", "free", "none"]
    signedQuery: NotRequired[str]


class PostDataText(TypedDict):
    type: Literal["text"]
    content: str  # dumped json, e.g. "content": "[\"\",\"unstyled\",[]]"
    modificator: Literal["", "BLOCK_END"] | str  # BLOCK_END aka <br />, has no content  # noqa: PYI051


# NOTE: Examples: ["Text", "unstyled", []], ["Bold text", "unstyled", [[0, 0, 6]]]
type PostDataTextParsed = tuple[str, Literal["unstyled"] | str, Sequence[Sequence[int]]]  # noqa: PYI051


class PostDataLink(TypedDict):
    type: Literal["link"]
    content: str  # e.g. "[\"https://...\",\"unstyled\",[]]"
    explicit: bool
    url: str


class PostDataFile(TypedDict):
    type: Literal["file"]
    complete: bool
    id: str  # UUID
    isMigrated: bool
    size: int
    title: str
    url: str  # NOTE: url is https://cdn.boosty.to/file/00000000-0000-0000-0000-0000000000000 need migrate to https://cdn.boosty.to/file/00000000-0000-0000-0000-0000000000000?user_id=0000000&content_id=00000000-0000-0000-0000-0000000000000&expire_time=0000000000&sign=0000000000000000000000000000000000000000000000000000000000000000&is_migrated=true


type PostDataVideoURLsType = Literal[
    "live_cmaf",
    "live_playback_dash",
    "ultra_hd",
    "live_playback_hls",
    "hls",
    "live_ondemand_hls",
    "lowest",
    "tiny",
    "high",
    "medium",
    "full_hd",
    "live_dash",
    "dash_uni",
    "low",
    "live_hls",
    "quad_hd",
    "dash",
]


class PostDataVideoURLs(TypedDict):
    """
    NOTE: seems like when video does not have specified type (quality preset), url = ""

    Regular static .mp4 ~720p upload have this types: ["hls", "lowest", "tiny', "medium", "low", "dash"]

    URL look like this:
    https://vd123.okcdn.ru/video.m3u8?cmd=videoPlayerCdn&expires=0000000000000&srcIp=1.2.3.4&pr=12&srcAg=UNKNOWN&ms=1.2.3.4&type=2&sig=00000000000&ct=8&urls=1.2.3.4&clientType=18&id=0000000000000
    https://vd123.okcdn.ru/?expires=0000000000000&srcIp=1.2.3.4&pr=42&srcAg=UNKNOWN&ms=1.2.3.4&type=0&sig=00000000000&ct=0&urls=1.2.3.4&clientType=18&id=0000000000000

    Where:
    * expires - unix timestamp
    * srcIp, ms, urls - 3 different IPs
    * pr - seems always = 42
    * ct - seems always = 0 for static types and 8 for hls, 6 for dash
    * clientType - seems always = 18
    * srcAg - seems always = UNKNOWN
    * type - is integer representation of text type
        * hls = 2
        * lowest = 0
        * tiny = 4
        * medium = 2
        * low = 1
        * dash = 1

    Approximate priority:
    ultra_hd > quad_hd > full_hd > high > medium > tiny > low > lowest
    hls and dash seems to use highest quality
    """

    type: PostDataVideoURLsType
    url: str


class PostDataVideo(TypedDict):
    """
    Example JSON:

    ```json
    {
        "height":480,
        "showViewsCounter":true,
        "viewsCounter":0,
        "timeCode":5,
        "complete":true,
        "uploadStatus": "ok",
        "playerUrls": [
            {"type": "live_cmaf", "url": ""},
            {"url": "", "type": "live_playback_dash"},
            {"type": "ultra_hd", "url": ""},
            {"url": "", "type": "live_playback_hls"},
            {"type": "hls", "url": "https://vd123.okcdn.ru/video.m3u8?cmd=videoPlayerCdn&expires=0000000000000&srcIp=1.2.3.4&pr=42&srcAg=UNKNOWN&ms=1.2.3.4&type=2&sig=00000000000&ct=8&urls=1.2.3.4&clientType=18&id=0000000000000"},
            {"type": "live_ondemand_hls", "url": ""},
            {"url": "https://vd123.okcdn.ru/?expires=0000000000000&srcIp=1.2.3.4&pr=42&srcAg=UNKNOWN&ms=1.2.3.4&type=0&sig=00000000000&ct=0&urls=1.2.3.4&clientType=18&id=0000000000000", "type": "lowest"},
            {"url": "https://vd123.okcdn.ru/?expires=0000000000000&srcIp=1.2.3.4&pr=42&srcAg=UNKNOWN&ms=1.2.3.4&type=4&sig=00000000000&ct=0&urls=1.2.3.4&clientType=18&id=0000000000000", "type": "tiny"},
            {"type": "high", "url": ""},
            {"type": "medium", "url": "https://vd123.okcdn.ru/?expires=0000000000000&srcIp=1.2.3.4&pr=42&srcAg=UNKNOWN&ms=1.2.3.4&type=2&sig=00000000000&ct=0&urls=1.2.3.4&clientType=18&id=0000000000000"},
            {"url": "", "type": "full_hd"},
            {"type": "live_dash", "url": ""},
            {"type": "dash_uni", "url": ""},
            {"type": "low", "url": "https://vd123.okcdn.ru/?expires=0000000000000&srcIp=1.2.3.4&pr=42&srcAg=UNKNOWN&ms=1.2.3.4&type=1&sig=00000000000&ct=0&urls=1.2.3.4&clientType=18&id=0000000000000"},
            {"type": "live_hls", "url": ""},
            {"type": "quad_hd", "url": ""},
            {"url": "https://vd123.okcdn.ru/?expires=0000000000000&srcIp=1.2.3.4&pr=42&srcAg=UNKNOWN&ms=1.2.3.4&type=1&sig=00000000000&ct=6&urls=1.2.3.4&clientType=18&id=0000000000000", "type": "dash"}
        ],
        "duration":34,
        "vid": "0000000000000",
        "width": 754,
        "failoverHost": "vd234.okcdn.ru",
        "preview": "https://i.okcdn.ru/videoPreview?id=0000000000000&type=39&idx=13&tkn=0000000000000000-0000000000",
        "defaultPreview": "https://i.okcdn.ru/videoPreview?id=0000000000000&type=39&idx=13&tkn=0000000000000000-0000000000",
        "url": "",
        "title": "Title",
        "type": "ok_video",
        "id": "00000000-0000-0000-0000-000000000000"
    }
    ```
    """  # noqa: E501

    type: Literal["ok_video"]
    complete: bool
    id: str
    height: int
    showViewsCounter: bool
    viewsCounter: NotRequired[int]
    timeCode: int
    uploadStatus: Literal["ok"] | str  # noqa: PYI051 # TODO: other statuses?
    playerUrls: Sequence[PostDataVideoURLs]
    duration: NotRequired[int]
    vid: str
    width: int
    failoverHost: NotRequired[str]
    preview: str
    defaultPreview: str
    url: NotRequired[str]
    title: str


class PostDataAudio(TypedDict):
    """
    Example JSON:

    ```json
    {
        "uploadStatus": "",
        "timeCode": 0,
        "complete": True,
        "size": 7526176,
        "viewsCounter": 0,
        "showViewsCounter": True,
        "track": "Day and Night",
        "duration": 188,
        "url": "https://cdn.boosty.to/audio/b46f05b1-c6d1-4a10-8791-6a65077aa1c2",
        "album": "Shiki Original Soundtrack Mini Album ''Rouge''",
        "title": "Day and Night.mp3",
        "artist": "Yasuharu Takanashi",
        "id": "b46f05b1-c6d1-4a10-8791-6a65077aa1c2",
        "isMigrated": True,
        "fileType": "MP3",
        "type": "audio_file"
    }
    ```
    """

    type: Literal["audio_file"]
    complete: bool
    id: str
    isMigrated: bool
    uploadStatus: str
    timeCode: int
    size: int
    viewsCounter: NotRequired[int]
    showViewsCounter: bool
    track: NotRequired[str]  # ? from file metadata
    duration: int
    url: str
    album: NotRequired[str]  # ? from file metadata
    title: str  # ? filename
    artist: NotRequired[str]  # ? from file metadata
    fileType: Literal["MP3"] | str  # noqa: PYI051 # TODO: other types?


class PostDataImage(TypedDict):
    """NOTE: When file deleted from CDN, "width", "height", "size" are not present and any image URL redirect to https://images.boosty.to/stubs/default.png"""

    type: Literal["image"]
    id: str  # UUID
    height: NotRequired[int]
    rendition: str
    size: NotRequired[int]
    url: str  # e.g. "https://images.boosty.to/image/00000000-0000-0000-0000-000000000000?change_time=0000000000" - unix time
    width: NotRequired[int]


class Posts(TypedDict):
    """
    Only nessesary fields are typed, others are omitted

    "signedQuery" not present when authentication is not successful
    """

    id: str  # UUID
    int_id: int
    title: str
    hasAccess: bool
    data: Sequence[PostDataText | PostDataLink | PostDataFile | PostDataVideo | PostDataAudio | PostDataImage]
    signedQuery: NotRequired[str]
    poll: NotRequired[dict]


class PostsExtra(TypedDict):
    offset: str
    isLast: bool


class PostsResponse(TypedDict):
    data: Sequence[Posts]
    extra: PostsExtra


@dataclass
class ProgressContext:
    progress: Progress
    posts: TaskID
    users: TaskID | None
    progress_download: Progress
    download: TaskID
    total: int
    group: Group


# ? Methods


def handle_file(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    int_id: int,
    title: str,
    incremental_id: int,
    url: str,
    filename: str,
    size: int | None = None,
    is_migrated: bool,
    user: str,
    output_dir: Path,
    signed_query: str,
    ctx: ProgressContext,
    force_redownload: bool = False,
    db_conn: sqlite3.Connection | None = None,
) -> None:
    """
    Handle file downloads
    """

    final_url = f"{url}{signed_query}&is_migrated={str(is_migrated).lower()}"
    path = output_dir / f"{int_id}_{title}_{incremental_id}_{filename}"
    entry = f"boosty_{user}_{int_id}_{incremental_id}"

    if size is None:
        ctx.progress.print("[yellow]Skipping downloading deleted file from CDN:[/yellow]", final_url)
        return

    if not force_redownload and db_conn:
        with db_conn as cur, suppress(ValueError, sqlite3.Error):
            [[check]] = cur.execute(CHECK_ENTRY.format(entry=entry))
            if check:
                ctx.progress.print(f"[yellow]Skipping downloaded file ({size:_} B):[/yellow]", url, "(DB)")
                return

    elif not force_redownload and path.exists() and path.stat().st_size == size:
        ctx.progress.print(f"[yellow]Skipping downloaded file ({size:_} B):[/yellow]", final_url)
        return

    try:
        with client.stream("GET", final_url, headers=headers, timeout=60.0) as stream:
            if stream.is_server_error:
                ctx.progress.print("Get server error:", stream.status_code, "Retrying after 5 seconds...")
                sleep(5.0)
                handle_file(
                    client=client,
                    headers=headers,
                    int_id=int_id,
                    title=title,
                    incremental_id=incremental_id,
                    url=url,
                    filename=filename,
                    size=size,
                    is_migrated=is_migrated,
                    user=user,
                    output_dir=output_dir,
                    signed_query=signed_query,
                    ctx=ctx,
                    force_redownload=force_redownload,
                    db_conn=db_conn,
                )
                return
            if not stream.is_success:
                rich.inspect(stream, title="Downloading file error")
                # rich.inspect(stream.request)
                return

            with path.open("wb") as f:
                total = int(stream.headers["Content-Length"])

                ctx.progress_download.start_task(ctx.download)
                ctx.progress_download.update(ctx.download, total=total, visible=True)
                ctx.progress.print(f"[green]Downloading ({size:_} B):[/green]", final_url)

                for chunk in stream.iter_bytes():
                    f.write(chunk)
                    ctx.progress_download.update(ctx.download, completed=stream.num_bytes_downloaded)

                ctx.progress_download.update(ctx.download, completed=0, visible=False)
                ctx.progress_download.stop_task(ctx.download)
    except httpx.TimeoutException:
        ctx.progress.print(f"[red italic]Timeout exception: {final_url}[/red italic]")
        return
    except httpx.NetworkError as e:
        rich.inspect(e, title=f"Network error: {final_url}")
        return
    except httpx.ProtocolError as e:
        rich.inspect(e, title=f"Protocol error: {final_url}")
        return
    except httpx.StreamError as e:
        rich.inspect(e, title=f"Streaming file error: {final_url}")
        return

    if db_conn:
        with db_conn as cur, suppress(sqlite3.Error):
            cur.execute(INSERT_ENTRY.format(entry=entry))
            cur.commit()


def handle_image(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    int_id: int,
    title: str,
    incremental_id: int,
    url: str,
    filename: str,
    user: str,
    output_dir: Path,
    width: int | None = None,
    height: int | None = None,
    size: int | None = None,
    ctx: ProgressContext,
    force_redownload: bool = False,
    db_conn: sqlite3.Connection | None = None,
) -> None:
    """
    Handle image downloads. Extension is not known, so has to be guessed with magic
    """

    if width is None or height is None or size is None:
        ctx.progress.print("[yellow]Skipping downloading deleted image from CDN:[/yellow]", url)
        return

    entry = f"boosty_{user}_{int_id}_{incremental_id}"

    if not force_redownload and db_conn:
        with db_conn as cur, suppress(ValueError, sqlite3.Error):
            [[check]] = cur.execute(CHECK_ENTRY.format(entry=entry))
            if check:
                ctx.progress.print(f"[yellow]Skipping downloaded image ({size:_} B):[/yellow]", url, "(DB)")
                return

    try:
        with client.stream("GET", url, headers=headers, timeout=60.0) as stream:
            if stream.is_server_error:
                ctx.progress.print("Get server error:", stream.status_code, "Retrying after 5 seconds...")
                sleep(5.0)
                handle_image(
                    client=client,
                    headers=headers,
                    int_id=int_id,
                    title=title,
                    incremental_id=incremental_id,
                    url=url,
                    filename=filename,
                    user=user,
                    output_dir=output_dir,
                    width=width,
                    height=height,
                    size=size,
                    ctx=ctx,
                    force_redownload=force_redownload,
                    db_conn=db_conn,
                )
                return
            if not stream.is_success:
                rich.inspect(stream, title="Downloading image error")
                return

            total = int(stream.headers["Content-Length"])

            ctx.progress_download.start_task(ctx.download)
            ctx.progress_download.update(ctx.download, total=total, visible=True)

            iterator: Iterator[bytes] = stream.iter_bytes(chunk_size=16_384)

            chunk: bytes = next(iterator)
            mime_type: str = magic.from_buffer(chunk, mime=True)
            extension: str = MIME_TO_EXTENSION.get(mime_type, "png")

            path = output_dir / f"{int_id}_{title}_{incremental_id}_{filename}.{extension}"

            if not force_redownload and not db_conn and path.exists() and path.stat().st_size == size:
                ctx.progress.print(f"[yellow]Skipping downloaded image ({size:_} B):[/yellow]", url)
                return

            ctx.progress.print(f"[green]Downloading ({size:_} B):[/green]", url)

            with path.open("wb") as f:
                f.write(chunk)
                for chunk in iterator:
                    f.write(chunk)
                    ctx.progress_download.update(ctx.download, completed=stream.num_bytes_downloaded)

            ctx.progress_download.update(ctx.download, completed=0, visible=False)
            ctx.progress_download.stop_task(ctx.download)
    except httpx.TimeoutException:
        ctx.progress.print(f"[red italic]Timeout exception: {url}[/red italic]")
        return
    except httpx.NetworkError as e:
        rich.inspect(e, title=f"Network error: {url}")
        return
    except httpx.ProtocolError as e:
        rich.inspect(e, title=f"Protocol error: {url}")
        return
    except httpx.StreamError as e:
        rich.inspect(e, title=f"Streaming image error: {url}")
        return

    if db_conn:
        with db_conn as cur, suppress(sqlite3.Error):
            cur.execute(INSERT_ENTRY.format(entry=entry))
            cur.commit()


def best_video(urls: dict[PostDataVideoURLsType, str], /) -> tuple[str, str] | tuple[None, None]:
    """Return best video quality"""

    for q in VIDEO_QUALITY_ORDER:
        if q in urls:
            return (q, urls[q])

    return (None, None)


def handle_video(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    int_id: int,
    title: str,
    incremental_id: int,
    urls: dict[PostDataVideoURLsType, str],
    failover_host: str | None = None,
    filename: str,
    user: str,
    output_dir: Path,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
    ctx: ProgressContext,
    force_redownload: bool = False,
    db_conn: sqlite3.Connection | None = None,
    retry: int = 0,
) -> None:
    """
    Handle video downloads. Extension is not known, so has to be guessed with magic
    """

    if width is None or height is None:  # or duration is None:
        ctx.progress.print("[yellow]Skipping downloading deleted video from CDN:[/yellow]", urls)
        return

    entry = f"boosty_{user}_{int_id}_{incremental_id}"

    if not force_redownload and db_conn:
        with db_conn as cur, suppress(ValueError, sqlite3.Error):
            [[check]] = cur.execute(CHECK_ENTRY.format(entry=entry))
            if check:
                ctx.progress.print(f"[yellow]Skipping downloaded video ({width}x{height} {duration or 0}s):[/yellow]", urls, "(DB)")
                return

    quality, url = best_video(urls)
    if not url or not quality:
        ctx.progress.print("[red]Not found supported video quality[/red]", urls)
        return

    # ? retry using failover host
    if retry >= 5 and failover_host:
        url = re.sub(r"(?<=https:\/\/)[\w.\-]+(?=/)", failover_host, url)
        ctx.progress.print("Trying downloading video using failover host:", failover_host)

    try:
        with client.stream("GET", url, headers=headers, timeout=60.0) as stream:
            if stream.is_server_error:
                ctx.progress.print("Get server error:", stream.status_code, "Retrying after 5 seconds...")
                sleep(5.0)
                handle_video(
                    client=client,
                    headers=headers,
                    int_id=int_id,
                    title=title,
                    incremental_id=incremental_id,
                    urls=urls,
                    failover_host=failover_host,
                    filename=filename,
                    user=user,
                    output_dir=output_dir,
                    width=width,
                    height=height,
                    duration=duration,
                    ctx=ctx,
                    force_redownload=force_redownload,
                    db_conn=db_conn,
                    retry=retry + 1,
                )
                return
            if not stream.is_success:
                rich.inspect(stream, title="Downloading video error")
                return

            total = int(stream.headers["Content-Length"])

            ctx.progress_download.start_task(ctx.download)
            ctx.progress_download.update(ctx.download, total=total, visible=True)

            iterator: Iterator[bytes] = stream.iter_bytes(chunk_size=16_384)

            chunk: bytes = next(iterator)
            mime_type: str = magic.from_buffer(chunk, mime=True)
            extension: str = MIME_TO_EXTENSION.get(mime_type, "mp4")

            path = output_dir / f"{int_id}_{title}_{incremental_id}_{filename}.{quality}.{extension}"

            if not force_redownload and not db_conn and path.exists() and path.stat().st_size > 0:
                ctx.progress.print(f"[yellow]Skipping downloaded video ({width}x{height} {duration or 0}s):[/yellow]", url)
                return

            ctx.progress.print(f"[green]Downloading ({width}x{height} {duration or 0}s):[/green]", url)

            with path.open("wb") as f:
                f.write(chunk)
                for chunk in iterator:
                    f.write(chunk)
                    ctx.progress_download.update(ctx.download, completed=stream.num_bytes_downloaded)

            ctx.progress_download.update(ctx.download, completed=0, visible=False)
            ctx.progress_download.stop_task(ctx.download)
    except httpx.TimeoutException:
        ctx.progress.print(f"[red italic]Timeout exception: {url}[/red italic]")
        return
    except httpx.NetworkError as e:
        rich.inspect(e, title=f"Network error: {url}")
        return
    except httpx.ProtocolError as e:
        rich.inspect(e, title=f"Protocol error: {url}")
        return
    except httpx.StreamError as e:
        rich.inspect(e, title=f"Streaming video error: {url}")
        return

    if db_conn:
        with db_conn as cur, suppress(sqlite3.Error):
            cur.execute(INSERT_ENTRY.format(entry=entry))
            cur.commit()


def handle_audio(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    int_id: int,
    title: str,
    incremental_id: int,
    url: str,
    filename: str,
    filename_fallback: str,
    file_type: str | None = None,
    size: int | None = None,
    is_migrated: bool,
    user: str,
    output_dir: Path,
    signed_query: str,
    ctx: ProgressContext,
    force_redownload: bool = False,
    db_conn: sqlite3.Connection | None = None,
) -> None:
    """
    Handle audio downloads. Extension is within a title, however if not, there is file_type in CAPS from API.
    """

    if size is None:
        ctx.progress.print("[yellow]Skipping downloading deleted audio from CDN:[/yellow]", url)
        return

    if not filename:
        filename = f"{filename_fallback}.{(file_type or "mp3").lower()}"

    final_url = f"{url}{signed_query}&is_migrated={str(is_migrated).lower()}"
    path = output_dir / f"{int_id}_{title}_{incremental_id}_{filename}"
    entry = f"boosty_{user}_{int_id}_{incremental_id}"

    if not force_redownload and db_conn:
        with db_conn as cur, suppress(ValueError, sqlite3.Error):
            [[check]] = cur.execute(CHECK_ENTRY.format(entry=entry))
            if check:
                ctx.progress.print(f"[yellow]Skipping downloaded audio ({size:_} B):[/yellow]", url, "(DB)")
                return

    try:
        with client.stream("GET", final_url, headers=headers, timeout=60.0) as stream:
            if stream.is_server_error:
                ctx.progress.print("Get server error:", stream.status_code, "Retrying after 5 seconds...")
                sleep(5.0)
                handle_audio(
                    client=client,
                    headers=headers,
                    int_id=int_id,
                    title=title,
                    incremental_id=incremental_id,
                    url=url,
                    filename=filename,
                    filename_fallback=filename_fallback,
                    file_type=file_type,
                    size=size,
                    is_migrated=is_migrated,
                    user=user,
                    output_dir=output_dir,
                    signed_query=signed_query,
                    ctx=ctx,
                    force_redownload=force_redownload,
                    db_conn=db_conn,
                )
                return
            if not stream.is_success:
                rich.inspect(stream, title="Downloading audio error")
                # rich.inspect(stream.request)
                return

            with path.open("wb") as f:
                total = int(stream.headers["Content-Length"])

                ctx.progress_download.start_task(ctx.download)
                ctx.progress_download.update(ctx.download, total=total, visible=True)
                ctx.progress.print(f"[green]Downloading ({size:_} B):[/green]", final_url)

                for chunk in stream.iter_bytes():
                    f.write(chunk)
                    ctx.progress_download.update(ctx.download, completed=stream.num_bytes_downloaded)

                ctx.progress_download.update(ctx.download, completed=0, visible=False)
                ctx.progress_download.stop_task(ctx.download)
    except httpx.TimeoutException:
        ctx.progress.print(f"[red italic]Timeout exception: {final_url}[/red italic]")
        return
    except httpx.NetworkError as e:
        rich.inspect(e, title=f"Network error: {final_url}")
        return
    except httpx.ProtocolError as e:
        rich.inspect(e, title=f"Protocol error: {final_url}")
        return
    except httpx.StreamError as e:
        rich.inspect(e, title=f"Streaming audio error: {final_url}")
        return

    if db_conn:
        with db_conn as cur, suppress(sqlite3.Error):
            cur.execute(INSERT_ENTRY.format(entry=entry))
            cur.commit()


def parse_text(raw_text: str) -> str:
    """
    Get text from stringified JSON
    """

    try:
        text_obj: PostDataTextParsed = orjson.loads(raw_text or """["", "unstyled", []]""")
        return text_obj[0]
    except (orjson.JSONDecodeError, IndexError):
        rich.inspect(raw_text, title="Error parsing post text JSON")
        return ""


def clear_post_text(post_text: Sequence[PostDataText | PostDataLink]) -> str:
    """
    Return human-readable post text

    NOTE: Links have similar to markdown content where [text](https://url), however hidden links also persist where content == "", they should be stripped
    Regular text have content encoded in stringified JSON, like ["Text", "unstyled", []] or ["Bold text", "unstyled", [[0, 0, 6]]]
    Modifiers are typically = "BLOCK_END" which is new line, <br />
    """

    return "".join(
        ((d["url"] if parse_text(d["content"]) != "" else "") if d["type"] == "link" else (parse_text(d["content"]) if d["type"] == "text" and d["modificator"] == "" else "\n"))
        for d in post_text
    )


def clear_filename(filename: str) -> str:
    """
    Clear filename from forbidden characters

    * Remove spaces around file name
    * Remove dot at the end of name (if no extension)
    * Remove control characters
    * Replace forbidden characters for unicode alternative
    """

    filename = filename.strip().strip(".")

    for [replacement, replacer] in FILENAME_REPLACEMENTS:
        filename = filename.replace(replacement, replacer)

    return FILENAME_CONTROLS.sub("", filename)


def handle_posts(
    client: httpx.Client,
    headers: dict[str, str],
    posts: Sequence[Posts],
    user: str,
    output_dir: Path,
    # user_id: int,
    *,
    force_redownload: bool = False,
    all_links: deque[tuple[str, str, str]],
    signed_query: str = "?t",
    db_conn: sqlite3.Connection | None = None,
    ctx: ProgressContext,
) -> None:
    """
    Handle posts
    """

    for post in posts:
        if _sq := post.get("signedQuery"):
            signed_query = _sq

        post_id: str = post["id"]
        int_id: int = post["int_id"]
        title: str = clear_filename(post["title"])
        has_access = post["hasAccess"]
        data = post["data"]
        incremental_id = 0
        post_text: list[PostDataText | PostDataLink] = []
        dl_tasks: list = []
        # found_password: bool = False

        if not has_access:
            ctx.progress.print(f"Skipping post {post['id']} ({post["int_id"]}) - has no access\n")
            ctx.total -= 1
            ctx.progress.update(ctx.posts, total=ctx.total)
            continue

        for d in data:
            if d["type"] == "file":
                dl_tasks.append(
                    partial(
                        handle_file,
                        client=client,
                        headers=headers,
                        int_id=int_id,
                        title=title,
                        incremental_id=incremental_id,
                        url=d["url"],
                        filename=clear_filename(d["title"]),
                        size=d["size"],
                        is_migrated=d["isMigrated"],
                        user=user,
                        output_dir=output_dir,
                        ctx=ctx,
                        signed_query=signed_query,
                        force_redownload=force_redownload,
                        db_conn=db_conn,
                    ),
                )
                incremental_id += 1
            elif d["type"] == "image":
                dl_tasks.append(
                    partial(
                        handle_image,
                        client=client,
                        headers=headers,
                        int_id=int_id,
                        title=title,
                        incremental_id=incremental_id,
                        url=d["url"],
                        filename=d["id"],
                        user=user,
                        output_dir=output_dir,
                        width=d.get("width"),
                        height=d.get("height"),
                        size=d.get("size"),
                        ctx=ctx,
                        force_redownload=force_redownload,
                        db_conn=db_conn,
                    ),
                )
                incremental_id += 1
            elif d["type"] == "link":
                post_text.append(d)
                all_links.appendleft((str(int_id), f"https://boosty.to/{user}/posts/{post_id}", d["url"]))
                # ctx.progress.print(f"Found link in post: https://boosty.to/{user}/posts/{post_id}", d["url"])
            elif d["type"] == "text":
                post_text.append(d)
                # NOTE: v2 removed alering only on passwords, save all text content instead
                # if not ((raw_text := d["content"]) and d["modificator"] == ""):
                #     continue

                # if "password" in parse_text(raw_text).lower():
                #     found_password = True
            elif d["type"] == "ok_video":
                dl_tasks.append(
                    partial(
                        handle_video,
                        client=client,
                        headers=headers,
                        int_id=int_id,
                        title=title,
                        incremental_id=incremental_id,
                        urls={u["type"]: u["url"] for u in d["playerUrls"] if u["url"]},
                        filename=d["id"],
                        user=user,
                        output_dir=output_dir,
                        width=d.get("width"),
                        height=d.get("height"),
                        failover_host=d.get("failoverHost"),
                        ctx=ctx,
                        force_redownload=force_redownload,
                        db_conn=db_conn,
                    ),
                )
                incremental_id += 1
            elif d["type"] == "audio_file":
                dl_tasks.append(
                    partial(
                        handle_audio,
                        client=client,
                        headers=headers,
                        int_id=int_id,
                        title=title,
                        incremental_id=incremental_id,
                        url=d["url"],
                        filename=d.get("title"),
                        filename_fallback=d["id"],
                        file_type=d.get("fileType"),
                        is_migrated=d["isMigrated"],
                        user=user,
                        output_dir=output_dir,
                        size=d.get("size"),
                        ctx=ctx,
                        signed_query=signed_query,
                        force_redownload=force_redownload,
                        db_conn=db_conn,
                    ),
                )
                incremental_id += 1
            else:
                ctx.progress.print("\n\n[red italic]Unsupported data type:[/red italic]", d["type"])
                rich.inspect(d, title="Unsupported data example", docs=False)
                ctx.progress.print(end="\n\n")

        # if found_password:
        cleared_post_text = clear_post_text(post_text).strip()
        ctx.progress.print(Panel(cleared_post_text, title=f"https://boosty.to/{user}/posts/{post_id}", highlight=True, padding=(0, 1), style="inspect.value.border"))

        if cleared_post_text:
            with (Path(user) / f"{int_id}_{title}.txt").open("w", encoding="utf-8") as f:
                f.write(cleared_post_text)

        for task in dl_tasks:
            task()

        ctx.progress.advance(ctx.posts)
        ctx.progress.print()


def archive_user(
    url: str,
    *,
    output_dir: Path | None = None,
    force_redownload: bool = False,
    token: str | None = None,
    cookies: Path | None = None,
    use_db: bool = False,
    db_path: Path | None = None,
    ctx: ProgressContext,
) -> None:
    """
    Archiving user by URL / user name
    """

    if not (match := USER_CHECK.match(url)):
        ctx.progress.print("URL is not supported")
        return

    user: str = match.group("user")
    cookies_path = cookies or (Path(__file__).parent / "cookies.txt")
    if not cookies_path.exists():
        ctx.progress.print(""""cookies.txt" file is not exists, save cookies for boosty.to domain""")
        return

    jar = MozillaCookieJar(cookies_path)
    jar.load()

    if not token:
        token_path = Path(__file__).parent / "token.txt"
        if not token_path.exists():
            ctx.progress.print(""""token.txt" file is not exists (header: "Authorization: Bearer __token__")""")
            return
        with token_path.open() as f:
            token = f.read()

    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}

    ctx.progress.print("Starting downloading user:", user)

    with (
        httpx.Client(cookies=jar, headers=headers, transport=httpx.HTTPTransport(retries=5)) as client,
        closing(sqlite3.connect((db_path or DB_PATH) if use_db else ":memory:")) as conn,
    ):
        with conn as cur, suppress(sqlite3.Error):
            cur.execute("CREATE TABLE IF NOT EXISTS archive (entry PRIMARY KEY) WITHOUT ROWID;")
            cur.commit()

        # NOTE: v2 moved getting "signedQuery" to individual posts
        # r: httpx.Response = client.get(f"https://boosty.to/{user}", headers=headers)
        # if not r.is_success:
        #     rich.inspect(r, title="Getting initial data result in error")
        #     return

        # html = BeautifulSoup(r.text, "html5lib")
        # if not (script := html.select_one("script#initial-state")):
        #     ctx.progress.print("Script is not found on blog page")
        #     return
        # if not script.string or not (_initial_state := str(script.string)):
        #     rich.inspect(script, title="Script with initial state text is empty, expected JSON")
        #     return

        # initial_state: InitialState = orjson.loads(_initial_state)
        # signed_query: str = initial_state["blog"]["blog"]["data"]["signedQuery"]

        # NOTE: v3 remove dependency from current user
        # r: httpx.Response = client.get(API_GET_CURRENT_USER, headers=headers)
        # if not r.is_success:
        #     rich.inspect(r, title="Current User request result in error")
        #     return

        # self_user: CurrentUser = r.json()
        # user_id = self_user["id"]

        r = client.get(API_GET_USER.format(user=user), headers=headers)
        if r.is_server_error:
            ctx.progress.print("Get server error:", r.status_code, "Retrying after 5 seconds...")
            sleep(5.0)
            archive_user(url, output_dir=output_dir, token=token, cookies=cookies, use_db=use_db, db_path=db_path, ctx=ctx)
            return
        if not r.is_success:
            rich.inspect(r, title="User request result in error")
            return

        boosty_user: User = r.json()
        signed_query = boosty_user.get("signedQuery", "?t")

        all_links: deque[tuple[str, str, str]] = deque()

        if not (output_path := Path(output_dir or ".") / user).exists():
            output_path.mkdir(parents=True, exist_ok=True)

        ctx.progress.print("\n\nRequesting page 1:", API_GET_POSTS_FIRST.format(user=user), end="\n\n")

        r = client.get(API_GET_POSTS_FIRST.format(user=user), headers=headers)
        if r.is_server_error:
            ctx.progress.print("Get server error:", r.status_code, "Retrying after 5 seconds...")
            sleep(5.0)
            archive_user(url, output_dir=output_dir, token=token, cookies=cookies, use_db=use_db, db_path=db_path, ctx=ctx)
            return
        if not r.is_success:
            rich.inspect(r, title="Posts request on page 1 result in error")
            return

        posts: PostsResponse = r.json()
        posts_data = posts["data"]

        if ctx:
            ctx.total += len(posts_data)
            ctx.progress.update(ctx.posts, total=ctx.total)

        handle_posts(
            client=client,
            headers=headers,
            posts=posts_data,
            user=user,
            # user_id=user_id,
            output_dir=output_path,
            force_redownload=force_redownload,
            all_links=all_links,
            signed_query=signed_query,
            db_conn=conn if use_db else None,
            ctx=ctx,
        )

        offset: str = posts["extra"]["offset"]
        is_past_page: bool = posts["extra"]["isLast"]
        if is_past_page:
            return

        page = 2
        while not is_past_page:
            ctx.progress.print(f"\nRequesting page {page}:", API_GET_POSTS.format(user=user, offset=offset), end="\n\n")

            r = client.get(API_GET_POSTS.format(user=user, offset=offset), headers=headers)
            if r.is_server_error:
                ctx.progress.print("Get server error:", r.status_code, "Retrying after 5 seconds...")
                sleep(5.0)
                continue
            if not r.is_success:
                rich.inspect(r, title=f"Posts request on page {page} result in error:")
                return

            posts = r.json()
            posts_data = posts["data"]

            if ctx:
                ctx.total += len(posts_data)
                ctx.progress.update(ctx.posts, total=ctx.total)

            handle_posts(
                client=client,
                headers=headers,
                posts=posts_data,
                user=user,
                # user_id=user_id,
                output_dir=output_path,
                force_redownload=force_redownload,
                all_links=all_links,
                signed_query=signed_query,
                db_conn=conn if use_db else None,
                ctx=ctx,
            )

            offset = posts["extra"]["offset"]
            is_past_page = posts["extra"]["isLast"]
            page += 1

    if all_links:
        if (post_links := Path(user) / "_post_links.txt").exists():
            with post_links.open("r", encoding="utf-8") as f:
                for line in f:
                    if not (line := line.strip()):
                        continue

                    try:
                        pid, purl, lurl = line.split("\t", maxsplit=3)
                    except ValueError:
                        continue

                    exists = False
                    for [_pid, _purl, _lurl] in all_links:
                        if _pid == pid and _purl == purl and _lurl == lurl:
                            exists = True

                    if not exists:
                        all_links.append((pid, purl, lurl))

        all_links = deque(sorted(all_links, key=lambda x: x[0]))

        table = Table(title="All links from posts", show_header=True, expand=True, highlight=True)
        table.add_column("Post ID", overflow="fold")
        table.add_column("Post URL", overflow="fold")
        table.add_column("Link URL", overflow="fold")
        for [pid, purl, lurl] in all_links:
            table.add_row(str(pid), purl, lurl)
        ctx.progress.print(table)

        with post_links.open("w", encoding="utf-8") as f:
            f.writelines(f"{pid}\t{purl}\t{lurl}\n" for pid, purl, lurl in all_links)


def _version_callback(value: bool) -> None:
    if not value:
        return

    rich.print(f"Boosty archiver version: {__version__}")
    raise typer.Exit


def main(
    urls: Annotated[list[str], typer.Argument(help="URLs or user names from Boosty", show_default=False)],
    output_dir: Annotated[Path | None, typer.Option("--output_dir", "-O", help="Specify different output root directory", dir_okay=True, file_okay=False, show_default=".")] = None,
    force_redownload: Annotated[bool, typer.Option("--force-redownload", "-F", help="Do not skip files and redownload them again")] = False,
    token: Annotated[
        str | None,
        typer.Option("--token", "-T", help="""Specify "Authorization: Bearer __TOKEN__", otherwise load from "token.txt\"""", rich_help_panel="Authorization"),
    ] = None,
    cookies: Annotated[
        Path | None,
        typer.Option("--cookies", "-C", help="""Specify path to "cookies.txt" jar file""", show_default="cookies.txt", rich_help_panel="Authorization"),
    ] = None,
    use_db: Annotated[bool, typer.Option(help="Use sqlite3 DB file", rich_help_panel="Database")] = False,
    db_path: Annotated[Path | None, typer.Option(help="Specify custom DB path", rich_help_panel="Database")] = None,
    version: Annotated[bool | None, typer.Option("--version", "-V", help="""Shows archiver version in format "YYYY.MM.DD\"""", callback=_version_callback, is_eager=True)] = None,
) -> None:
    """
    Archive all users by URLs / user names
    """

    len_urls = len(urls)
    progress = Progress(
        "[progress.description]{task.description}",
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        expand=True,
    )
    posts = progress.add_task("Posts:", total=0)
    users = None
    if len_urls > 1:
        users = progress.add_task("Users:", total=len_urls)

    progress_download = Progress(
        "[progress.percentage]{task.percentage:>5.0f}%",
        BarColumn(bar_width=None),
        DownloadColumn(),
        TransferSpeedColumn(),
        transient=True,
    )
    download = progress_download.add_task("Download:", total=0, visible=False, start=False)

    group = Group(progress_download, progress)

    ctx = ProgressContext(progress=progress, posts=posts, users=users, progress_download=progress_download, download=download, total=0, group=group)

    with Live(group, refresh_per_second=15):
        for url in urls:
            archive_user(url, output_dir=output_dir, force_redownload=force_redownload, token=token, cookies=cookies, use_db=use_db, db_path=db_path, ctx=ctx)

            if users is not None:
                progress.advance(users)

        progress_download.stop_task(download)
        progress_download.update(download, visible=False)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        typer.run(main)
