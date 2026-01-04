import asyncio
import json
import re
from pathlib import Path
from random import choice
from re import Match
from typing import ClassVar, Any

import aiofiles
import msgspec
from msgspec import Struct, field

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, VideoContent, ImageContent
from ..download import Downloader
from .base import BaseParser, ParseException, handle


# ==========================================================
#  数据模型定义 (合并自 video.py 和 slides.py)
# ==========================================================

class Avatar(Struct):
    url_list: list[str]

class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None

class PlayAddr(Struct):
    url_list: list[str]

class Cover(Struct):
    url_list: list[str]

class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int

class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)

# --- 视频数据模型 ---
class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images] if self.images else []

    @property
    def video_url(self) -> str | None:
        return choice(self.video.play_addr.url_list).replace("playwm", "play") if self.video else None

    @property
    def cover_url(self) -> str | None:
        return choice(self.video.cover.url_list) if self.video else None

    @property
    def avatar_url(self) -> str | None:
        if avatar := self.author.avatar_thumb:
            return choice(avatar.url_list)
        elif avatar := self.author.avatar_medium:
            return choice(avatar.url_list)
        return None

class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)
    @property
    def video_data(self) -> VideoData:
        if len(self.item_list) == 0:
            raise ValueError("can't find data in videoInfoRes")
        return choice(self.item_list)

class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(name="videoInfoRes", default_factory=VideoInfoRes)

class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)

class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None
    @property
    def video_data(self) -> VideoData:
        if page := self.loader_data.video_page:
            return page.video_info_res.video_data
        elif page := self.loader_data.note_page:
            return page.video_info_res.video_data
        raise ValueError("can't find video_(id)/page or note_(id)/page in router data")

# --- 幻灯片数据模型 ---
class SlidesData(Struct):
    author: Author
    desc: str
    create_time: int
    images: list[Image]

    @property
    def name(self) -> str:
        return self.author.nickname

    @property
    def avatar_url(self) -> str:
        return choice(self.author.avatar_thumb.url_list)

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images]

    @property
    def dynamic_urls(self) -> list[str]:
        return [choice(image.video.play_addr.url_list) for image in self.images if image.video]

class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)


# ==========================================================
#  解析器逻辑
# ==========================================================

class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        
        # 1. 先定义 download_headers (必须在 _load_cookies 之前)
        # 修复：移除 Referer，防止 CDN 403 Forbidden
        self.download_headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        
        # 2. 更新请求 headers
        self.ios_headers["Referer"] = "https://www.douyin.com/"
        self.android_headers["Referer"] = "https://www.douyin.com/"
        
        # 3. 最后处理 Cookies
        self.douyin_ck = config.get("douyin_ck", "")
        self._cookies_file = Path(config["data_dir"]) / "douyin_cookies.json"
        
        # 异步加载 cookies 任务
        asyncio.create_task(self._init_cookies())

    async def _init_cookies(self):
        """异步初始化 Cookies"""
        await self._load_cookies()
        if self.douyin_ck:
            self._set_cookies(self.douyin_ck)

    def _clean_cookie(self, cookie: str) -> str:
        return cookie.replace("\n", "").replace("\r", "").strip()

    def _set_cookies(self, cookies: str):
        cleaned_cookies = self._clean_cookie(cookies)
        if cleaned_cookies:
            self.ios_headers["Cookie"] = cleaned_cookies
            self.android_headers["Cookie"] = cleaned_cookies
            # 关键：同时更新下载用的 headers
            self.download_headers["Cookie"] = cleaned_cookies

    async def _load_cookies(self):
        """异步加载 Cookies"""
        if not self._cookies_file.exists():
            return
        try:
            # 优化：使用 aiofiles 异步读取
            async with aiofiles.open(self._cookies_file, 'r', encoding='utf-8') as f:
                content = await f.read()
            
            cookies_data = json.loads(content)
            self.douyin_ck = cookies_data.get("cookie", "")
            if self.douyin_ck:
                self._set_cookies(self.douyin_ck)
                logger.info(f"已从 {self._cookies_file} 加载抖音 cookies")
        except Exception as e:
            logger.warning(f"加载抖音 cookies 失败: {e}")

    async def _save_cookies(self, cookies: str):
        """异步保存 Cookies"""
        try:
            # 优化：使用 aiofiles 异步写入
            async with aiofiles.open(self._cookies_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps({"cookie": cookies}, ensure_ascii=False))
            logger.info(f"已保存抖音 cookies 到 {self._cookies_file}")
        except Exception as e:
            logger.warning(f"保存抖音 cookies 失败: {e}")

    async def _update_cookies_from_response(self, set_cookie_headers: list[str]):
        if not set_cookie_headers:
            return

        existing_cookies = {}
        if self.douyin_ck:
            for cookie in self.douyin_ck.split(";"):
                cookie = cookie.strip()
                if cookie and "=" in cookie:
                    name, value = cookie.split("=", 1)
                    existing_cookies[name.strip()] = value.strip()

        for set_cookie in set_cookie_headers:
            cookie_part = set_cookie.split(";")[0].strip()
            if cookie_part and "=" in cookie_part:
                name, value = cookie_part.split("=", 1)
                existing_cookies[name.strip()] = value.strip()

        new_cookies = "; ".join([f"{k}={v}" for k, v in existing_cookies.items()])

        if new_cookies != self.douyin_ck:
            self.douyin_ck = new_cookies
            self._set_cookies(self.douyin_ck)
            await self._save_cookies(self.douyin_ck) # 异步调用

    # ==================== 短链处理 ====================
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # ==================== 长链处理 ====================
    
    # 支持 PC 端 modal_id 参数
    @handle("douyin", r"douyin\.com/.*[?&]modal_id=(?P<vid>\d+)")
    async def _parse_douyin_modal(self, searched: re.Match[str]):
        vid = searched.group("vid")
        logger.debug(f"[抖音] 解析 modal_id: {vid}")
        return await self._parse_mobile_first("video", vid)

    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("jingxuan.douyin", r"jingxuan\.douyin\.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")
        logger.debug(f"[抖音] 解析类型: {ty}, ID: {vid}")
        
        if ty == "slides":
            return await self.parse_slides(vid)
        
        # 优先使用移动端接口
        return await self._parse_mobile_first(ty, vid)

    async def _parse_mobile_first(self, ty: str, vid: str):
        """优先使用移动端接口，失败则回退到 yt-dlp"""
        urls = (
            f"https://m.douyin.com/share/{ty}/{vid}",
            f"https://www.iesdouyin.com/share/{ty}/{vid}",
        )

        for url in urls:
            try:
                logger.debug(f"[抖音] 尝试移动端接口: {url}")
                return await self.parse_video(url)
            except ParseException as e:
                logger.debug(f"[抖音] 移动端接口失败 {url}: {e}")
                continue
            except ValueError as e: # 捕获 msgspec 解析错误
                logger.debug(f"[抖音] 数据解析失败 {url}: {e}")
                continue

        logger.info(f"[抖音] 移动端接口失败，尝试 yt-dlp")
        return await self._parse_ytdlp_fallback(ty, vid)

    async def _parse_ytdlp_fallback(self, ty: str, vid: str):
        """yt-dlp 兜底"""
        url = f"https://www.douyin.com/{ty}/{vid}"
        
        try:
            info = await self.downloader.ytdlp_extract_info(url)
        except Exception as e:
            raise ParseException(f"所有解析方式均失败: {e}")

        contents = []
        
        if info.get("_type") == "playlist" and info.get("entries"):
            for entry in info["entries"]:
                img_url = entry.get("url")
                if img_url:
                    # 使用 download_headers
                    img_task = self.downloader.download_img(img_url, ext_headers=self.download_headers)
                    contents.append(ImageContent(img_task))
        elif info.get("duration"):
            title = info.get("title", "douyin_video")
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
            
            # 使用 download_headers
            video_task = self.downloader.download_video(
                url,
                use_ytdlp=True,
                video_name=safe_title
            )
            
            cover_task = None
            if info.get("thumbnail"):
                cover_task = self.downloader.download_img(info["thumbnail"], ext_headers=self.download_headers)
            
            contents.append(VideoContent(video_task, cover_task, duration=info.get("duration", 0)))

        author_name = info.get("uploader") or info.get("channel") or "抖音用户"

        return self.result(
            title=info.get("title", "抖音分享"),
            text=info.get("description", "")[:200] if info.get("description") else "",
            author=self.create_author(author_name),
            contents=contents,
            timestamp=info.get("timestamp"),
            url=info.get("webpage_url", url)
        )

    async def parse_with_redirect(self, url: str):
        """短链重定向"""
        async with self.client.get(
            url, headers=self.ios_headers, allow_redirects=False, ssl=False
        ) as resp:
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                await self._update_cookies_from_response(set_cookie_headers)

            redirect_url = url
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", url)
                logger.debug(f"[抖音] 重定向到: {redirect_url}")

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    async def parse_video(self, url: str):
        """解析视频/图文页面 (移动端接口)"""
        logger.debug(f"[抖音] 请求页面: {url}")

        async with self.client.get(
            url, headers=self.ios_headers, allow_redirects=False, ssl=False
        ) as resp:
            if resp.status != 200:
                raise ParseException(f"HTTP {resp.status}")
            text = await resp.text()
            
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                await self._update_cookies_from_response(set_cookie_headers)

        pattern = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
        matched = pattern.search(text)

        if not matched or not matched.group(1):
            raise ParseException("未找到 _ROUTER_DATA")

        video_data = msgspec.json.decode(matched.group(1).strip(), type=RouterData).video_data

        contents = []

        # 图文 - 使用 download_headers
        if image_urls := video_data.image_urls:
            for img_url in image_urls:
                img_task = self.downloader.download_img(img_url, ext_headers=self.download_headers)
                contents.append(ImageContent(img_task))
        # 视频
        elif video_url := video_data.video_url:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            
            video_task = self.downloader.download_video(video_url, ext_headers=self.download_headers)
            
            cover_task = None
            if cover_url:
                cover_task = self.downloader.download_img(cover_url, ext_headers=self.download_headers)
            
            contents.append(VideoContent(video_task, cover_task, duration=duration))

        author = self.create_author(
            video_data.author.nickname, 
            video_data.avatar_url, 
            ext_headers=self.download_headers
        )

        return self.result(
            title=video_data.desc,
            author=author,
            contents=contents,
            timestamp=video_data.create_time,
        )

    async def parse_slides(self, video_id: str):
        """解析幻灯片 (slides) 接口"""
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }

        async with self.client.get(
            url, params=params, headers=self.android_headers, ssl=False
        ) as resp:
            resp.raise_for_status()
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            if set_cookie_headers:
                await self._update_cookies_from_response(set_cookie_headers)

            response_text = await resp.read()
            slides_data = msgspec.json.decode(response_text, type=SlidesInfo).aweme_details[0]

        contents = []

        if image_urls := slides_data.image_urls:
            for img_url in image_urls:
                img_task = self.downloader.download_img(img_url, ext_headers=self.download_headers)
                contents.append(ImageContent(img_task))

        if dynamic_urls := slides_data.dynamic_urls:
            for dyn_url in dynamic_urls:
                dyn_task = self.downloader.download_video(dyn_url, ext_headers=self.download_headers)
                contents.append(ImageContent(dyn_task))

        author = self.create_author(
            slides_data.name, 
            slides_data.avatar_url, 
            ext_headers=self.download_headers
        )

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            timestamp=slides_data.create_time,
        )