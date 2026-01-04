import re
from re import Match
from typing import ClassVar

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, VideoContent, ImageContent
from ..download import Downloader
from .base import BaseParser, handle, ParseException


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="Twitter")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle("twitter.com", r"(?:(?:www|mobile)\.)?twitter\.com/([a-zA-Z0-9_]+)/status/(\d+)")
    @handle("x.com", r"(?:(?:www|mobile)\.)?x\.com/([a-zA-Z0-9_]+)/status/(\d+)")
    async def _parse_twitter(self, searched: Match[str]):
        user = searched.group(1)
        tweet_id = searched.group(2)
        
        api_url = f"https://api.vxtwitter.com/{user}/status/{tweet_id}"
        logger.info(f"[Twitter] 使用 VxTwitter API 解析: {api_url}")

        try:
            async with self.downloader.client.get(api_url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ParseException(f"API 请求失败: {resp.status} - {text}")
                data = await resp.json()
        except Exception as e:
            raise ParseException(f"连接 API 失败: {e}")

        if not data:
            raise ParseException("未获取到推文数据")

        text = data.get("text", "")
        timestamp = data.get("date_epoch")
        user_name = data.get("user_name", user)
        user_screen_name = data.get("user_screen_name", user)
        
        contents = []
        media_list = data.get("media_extended", [])

        for media in media_list:
            m_type = media.get("type")
            m_url = media.get("url")
            
            if m_type == "video" or m_type == "gif":
                video_task = self.downloader.download_video(
                    m_url, 
                    video_name=f"twitter_{tweet_id}",
                    proxy=self.config["proxy"]
                )
                cover_url = media.get("thumbnail_url")
                cover_task = None
                if cover_url:
                    cover_task = self.downloader.download_img(cover_url, proxy=self.config["proxy"])
                
                duration = media.get("duration_millis", 0) / 1000
                contents.append(VideoContent(video_task, cover_task, duration=duration))
                
            elif m_type == "image":
                img_task = self.downloader.download_img(m_url, proxy=self.config["proxy"])
                contents.append(ImageContent(img_task))

        author = self.create_author(f"{user_name} (@{user_screen_name})")

        return self.result(
            title="推特动态",
            text=text,
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=f"https://twitter.com/{user}/status/{tweet_id}",
        )