import re
from re import Match
from typing import ClassVar
from email.utils import parsedate_to_datetime

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, VideoContent, ImageContent
from ..download import Downloader
from .base import BaseParser, handle, ParseException


class WeiboParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="weibo", display_name="微博")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        # 伪装成 Android 客户端访问 m.weibo.cn
        self.headers.update({
            "Referer": "https://m.weibo.cn/",
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.162 Mobile Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "MWeibo-Pwa": "1",
            "X-Requested-With": "XMLHttpRequest"
        })

    @handle("weibo.com", r"weibo\.com/[0-9]+/([a-zA-Z0-9]+)")
    @handle("weibo.cn", r"weibo\.cn/(?:status|detail)/([a-zA-Z0-9]+)")
    async def _parse_weibo(self, searched: Match[str]):
        bid = searched.group(1)
        url = f"https://m.weibo.cn/statuses/show?id={bid}"
        
        logger.info(f"[Weibo] 尝试 API 解析: {url}")
        
        try:
            async with self.client.get(url, headers=self.headers) as resp:
                if resp.status != 200:
                    logger.warning(f"微博 API 请求失败: {resp.status}，尝试 fallback")
                    return await self._parse_with_ytdlp(searched.group(0))
                
                data = await resp.json()
        except Exception as e:
            logger.warning(f"连接微博 API 失败: {e}，尝试 fallback")
            return await self._parse_with_ytdlp(searched.group(0))

        if not data or data.get("ok") != 1:
            logger.warning(f"微博 API 返回错误 ({data.get('msg')})，尝试 fallback")
            return await self._parse_with_ytdlp(searched.group(0))

        data = data.get("data", {})
        if not data:
             raise ParseException("未获取到微博数据")

        # 1. 解析基础信息
        user = data.get("user", {})
        author_name = user.get("screen_name", "微博用户")
        author_avatar = user.get("profile_image_url", "")
        
        text = data.get("text", "")
        # 处理长文本
        if data.get("isLongText") and "longText" in data:
             text = data["longText"].get("longTextContent", text)
        
        # 简单清理 HTML 标签
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        
        # 解析时间
        timestamp = None
        if created_at := data.get("created_at"):
            try:
                dt = parsedate_to_datetime(created_at)
                timestamp = int(dt.timestamp())
            except Exception:
                pass
        
        contents = []

        # 2. 解析视频
        # 微博视频通常在 page_info 中
        page_info = data.get("page_info", {})
        if page_info and page_info.get("type") == "video":
            media_info = page_info.get("media_info", {})
            # 优先获取高清流
            video_url = (
                media_info.get("mp4_720p_mp4") or 
                media_info.get("mp4_hd_url") or 
                media_info.get("mp4_sd_url") or
                media_info.get("stream_url")
            )
            if video_url:
                cover_url = page_info.get("page_pic", {}).get("url")
                duration = media_info.get("duration", 0)
                
                # 关键修复：传递 ext_headers=self.headers 以带上 Referer
                video_task = self.downloader.download_video(
                    video_url, 
                    video_name=f"weibo_{bid}",
                    ext_headers=self.headers,
                    proxy=self.config["proxy"]
                )
                
                cover_task = None
                if cover_url:
                    # 关键修复：传递 ext_headers
                    cover_task = self.downloader.download_img(
                        cover_url, 
                        ext_headers=self.headers,
                        proxy=self.config["proxy"]
                    )
                    
                contents.append(VideoContent(video_task, cover_task, duration=duration))

        # 3. 解析图片 (如果是多图微博，且没有视频，或者作为补充)
        if not contents and "pics" in data:
            for pic in data["pics"]:
                # 优先获取大图
                large = pic.get("large", {})
                url = large.get("url") or pic.get("url")
                if url:
                    # 关键修复：传递 ext_headers
                    img_task = self.downloader.download_img(
                        url, 
                        ext_headers=self.headers,
                        proxy=self.config["proxy"]
                    )
                    contents.append(ImageContent(img_task))

        author = self.create_author(author_name, author_avatar, ext_headers=self.headers)
        
        # 构造原始链接 (PC端链接)
        original_url = f"https://weibo.com/{user.get('id')}/{bid}"

        return self.result(
            title="微博正文",
            text=text,
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=original_url,
        )

    async def _parse_with_ytdlp(self, url: str):
        """yt-dlp 兜底解析"""
        if not url.startswith("http"):
            url = f"https://{url}"
            
        logger.info(f"[Weibo] 使用 yt-dlp 兜底解析: {url}")
        
        # yt-dlp 会自动处理 headers
        info = await self.downloader.ytdlp_extract_info(url)
        contents = []
        
        if info.duration:
            video_task = self.downloader.download_video(
                url, 
                use_ytdlp=True, 
                proxy=self.config["proxy"],
                video_name=info.title
            )
            contents.append(VideoContent(video_task, None, duration=info.duration))
        
        if not contents and info.thumbnail:
            img_task = self.downloader.download_img(info.thumbnail, proxy=self.config["proxy"])
            contents.append(ImageContent(img_task))

        author = self.create_author(info.uploader or "微博用户")

        return self.result(
            title=info.title or "微博正文",
            text=info.description or "",
            author=author,
            contents=contents,
            timestamp=info.timestamp,
            url=url,
        )