import re
from re import Match
from typing import ClassVar

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, AudioContent, ImageContent
from ..download import Downloader
from .base import BaseParser, handle


class NCMParser(BaseParser):
    """网易云音乐解析器 (基于 yt-dlp)"""

    platform: ClassVar[Platform] = Platform(
        name="ncm", display_name="网易云"
    )

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle("163cn.tv", r"163cn\.tv/(?P<short_key>\w+)")
    async def _parse_short(self, searched: Match[str]):
        short_url = f"https://163cn.tv/{searched.group('short_key')}"
        return await self.parse_with_redirect(short_url)

    # 匹配 y.music.163.com (移动端)
    @handle("y.music.163.com", r"y\.music\.163\.com/m/song\?.*id=(?P<song_id>\d+)")
    # 匹配 music.163.com (PC端/Web端)
    # 优化后的正则：
    # 1. /+ 允许一个或多个斜杠
    # 2. (?:#/)? 允许可选的 hash 路由
    # 3. song.*?id= 允许 song 和 id 之间有任意字符（比如 ? 或 &）
    @handle("music.163.com", r"music\.163\.com/+(?:#/)?song.*?id=(?P<song_id>\d+)")
    async def _parse_song(self, searched: Match[str]):
        """使用 yt-dlp 解析网易云歌曲"""
        
        # 1. 提取 ID 并重组标准 URL
        # 这样做的好处是：无论原链接是 /#/song 还是 /song，或者是乱七八糟的参数
        # 只要提取到了 ID，我们就能构造出 yt-dlp 肯定能识别的干净链接
        song_id = searched.group("song_id")
        url = f"https://music.163.com/song?id={song_id}"

        logger.info(f"[NCM] 解析歌曲 ID: {song_id}, 重组链接: {url}")

        # 2. 提取信息 (yt-dlp)
        info = await self.downloader.ytdlp_extract_info(url)
        
        contents = []
        
        # 3. 处理文件名
        title = info.title or f"ncm_{song_id}"
        # 去除非法字符
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)

        # 4. 下载音频
        audio_task = self.downloader.download_audio(
            url, 
            use_ytdlp=True, 
            proxy=self.config["proxy"],
            audio_name=safe_title
        )
        contents.append(AudioContent(audio_task, duration=info.duration or 0))

        # 5. 下载封面
        if info.thumbnail:
            cover_task = self.downloader.download_img(info.thumbnail, proxy=self.config["proxy"])
            contents.append(ImageContent(cover_task))

        # 6. 构建作者信息
        author = self.create_author(info.author_name)

        return self.result(
            title=info.title,
            text=info.description,
            author=author,
            contents=contents,
            timestamp=info.timestamp,
            url=url,
        )

    # 直链 mp3 (保持不变)
    @handle("music.126.net",r"https?://[^/]*music\.126\.net/.*\.mp3(?:\?.*)?$")
    async def _parse_direct_mp3(self, searched: Match[str]):
        url = searched.group(0)
        audio = self.create_audio_content(url)
        return self.result(
            title="网易云音乐",
            text="直链音频",
            contents=[audio],
            url=url,
        )

    # 私人直链 (保持不变)
    @handle(
        "music.163.com/song/media/outer/url",
        r"(https?://music\.163\.com/song/media/outer/url\?[^>\s]+)",
    )
    async def _parse_private_outer(self, searched: Match[str]):
        private_url = searched.group(0)
        audio = self.create_audio_content(private_url)
        return self.result(
            title="网易云音乐（私人直链）",
            text="直链音频",
            contents=[audio],
            url=private_url,
        )