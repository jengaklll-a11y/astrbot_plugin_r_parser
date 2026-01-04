import json
import re
import time
import asyncio
from re import Match
from typing import ClassVar
from curl_cffi import requests as cffi_requests

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, ImageContent
from ..download import Downloader
from .base import BaseParser, handle, ParseException


class NGAParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="nga", display_name="NGA")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Referer": "https://bbs.nga.cn/",
            "Connection": "keep-alive",
        }

    @handle("nga.178.com", r"nga\.178\.com/read\.php\?.*tid=(\d+)")
    @handle("bbs.nga.cn", r"bbs\.nga\.cn/read\.php\?.*tid=(\d+)")
    @handle("ngabbs.com", r"ngabbs\.com/read\.php\?.*tid=(\d+)")
    async def _parse_nga(self, searched: Match[str]):
        tid = searched.group(1)
        url = f"https://bbs.nga.cn/read.php?tid={tid}&lite=js&rand={int(time.time())}"
        
        logger.info(f"[NGA] 解析帖子: {url}")

        def _fetch_nga():
            cookies = {"guestJs": str(int(time.time()))}
            
            if custom_ck := self.config.get("nga_cookies"):
                for ck in custom_ck.split(";"):
                    if "=" in ck:
                        k, v = ck.strip().split("=", 1)
                        cookies[k.strip()] = v.strip()

            return cffi_requests.get(
                url,
                headers=self.headers,
                impersonate="chrome124",
                cookies=cookies,
                timeout=20,
                allow_redirects=True
            )

        try:
            resp = await asyncio.to_thread(_fetch_nga)
            
            content_bytes = resp.content
            try:
                html = content_bytes.decode("gbk")
            except UnicodeDecodeError:
                html = content_bytes.decode("utf-8", errors="ignore")

            if resp.status_code == 403:
                raise ParseException("NGA 拒绝访问 (403)，可能是 IP 风控或 Cookie 无效")

        except Exception as e:
            if isinstance(e, ParseException):
                raise e
            raise ParseException(f"NGA 网络请求失败: {e}")

        if "访客不能直接访问" in html:
            raise ParseException("NGA 限制访客访问，请检查 Cookie 是否正确填写")
        if "Server is too busy" in html:
            raise ParseException("NGA 服务器繁忙")

        # 提取 JSON
        match = re.search(r"window\.script_muti_get_var_store\s*=\s*(\{.*?\})\s*$", html, re.DOTALL)
        if not match:
             match = re.search(r"window\.script_muti_get_var_store\s*=\s*(\{.*)", html, re.DOTALL)

        if not match:
            logger.error(f"[NGA] Regex match failed. Content start: {html[:200]}")
            raise ParseException("无法从页面提取数据 (Regex mismatch)")

        json_str = match.group(1).strip()
        
        if "</script>" in json_str:
            json_str = json_str.split("</script>")[0].strip()
        
        if json_str.endswith(";"):
            json_str = json_str[:-1]

        try:
            # 关键修改：strict=False 允许 JSON 字符串中包含未转义的控制字符（如换行符）
            data = json.loads(json_str, strict=False)
            data_body = data.get("data", {})
        except json.JSONDecodeError as e:
            # 如果还是失败，尝试清理常见的非法字符
            try:
                # 替换非法控制字符，但保留换行
                cleaned_json = re.sub(r'[\x00-\x09\x0b-\x1f]', '', json_str)
                data = json.loads(cleaned_json, strict=False)
                data_body = data.get("data", {})
            except Exception:
                logger.error(f"[NGA] JSON Decode Error. Pos: {e.pos}. Context: {json_str[max(0, e.pos-20):e.pos+20]}")
                raise ParseException(f"NGA 数据解析失败 (JSON Error): {e}")

        thread_info = data_body.get("__T", {})
        replies = data_body.get("__R", {})
        
        if not thread_info:
             msg = data_body.get("__M", {}).get("error", {}).get("0")
             if msg:
                 raise ParseException(f"NGA 返回错误: {msg}")
             raise ParseException("未获取到帖子元数据")

        title = thread_info.get("subject", "NGA 帖子")
        
        main_post = replies.get("0")
        if not main_post:
             keys = list(replies.keys())
             if keys:
                 main_post = replies[keys[0]]
        
        if not main_post:
             raise ParseException("未找到主楼内容")

        author_id = main_post.get("authorid", 0)
        user_info = data_body.get("__U", {}).get(str(author_id), {})
        author_name = user_info.get("username", f"UID:{author_id}")
        
        content_html = main_post.get("content", "")
        
        contents = []
        
        def fix_img_url(u):
            if u.startswith("./"):
                return "https://img.nga.178.com/attachments" + u[1:]
            return u

        img_urls = re.findall(r'<img[^>]+src="([^">]+)"', content_html)
        img_urls += re.findall(r'\[img\](.*?)\[/img\]', content_html)
        
        unique_imgs = set()
        for img in img_urls:
            full_url = fix_img_url(img)
            if full_url.startswith("http") and "smile" not in full_url:
                if full_url not in unique_imgs:
                    unique_imgs.add(full_url)
                    contents.append(ImageContent(
                        self.downloader.download_img(full_url, proxy=self.config["proxy"])
                    ))

        text = content_html.replace("<br/>", "\n").replace("<br>", "\n")
        text = re.sub(r'<img[^>]+>', '', text)
        text = re.sub(r'\[img\].*?\[/img\]', '', text)
        text = re.sub(r'\[quote\].*?\[/quote\]', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        
        text = text.strip()
        if len(text) > 500:
            text = text[:500] + "..."

        return self.result(
            title=title,
            text=text,
            author=self.create_author(author_name),
            contents=contents,
            url=f"https://bbs.nga.cn/read.php?tid={tid}",
        )