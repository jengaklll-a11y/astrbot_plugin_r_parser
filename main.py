import nest_asyncio
nest_asyncio.apply()
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from itertools import chain
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.data import (
    AudioContent,
    Author,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
)
from .core.debounce import LinkDebouncer
from .core.download import Downloader
from .core.exception import (
    DownloadException,
    DownloadLimitException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .core.parsers import (
    BaseParser,
    BilibiliParser,
)
from .core.utils import extract_json_url, save_cookies_with_netscape


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=2)

        # 插件数据目录
        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_r_parser")
        config["data_dir"] = str(self.data_dir)

        # 缓存目录
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)
        self.config.save_config()

        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}

        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

        # 下载器
        self.downloader = Downloader(config)

        # 防抖器
        self.debouncer = LinkDebouncer(config)

        # 仲裁器
        self.arbiter = EmojiLikeArbiter()

        # 缓存清理器
        self.cleaner = CacheCleaner(self.context, self.config)

    # region 生命周期

    async def initialize(self):
        """加载、重载插件时触发"""
        # ytb_cookies
        if self.config["ytb_ck"]:
            ytb_cookies_file = self.data_dir / "ytb_cookies.txt"
            ytb_cookies_file.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                save_cookies_with_netscape,
                self.config["ytb_ck"],
                ytb_cookies_file,
                "youtube.com",
            )
            self.config["ytb_cookies_file"] = str(ytb_cookies_file)
            self.config.save_config()
        
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()
        # 关闭线程池
        self._executor.shutdown(wait=False)

    def _register_parser(self):
        """注册解析器"""
        # 获取所有解析器
        all_subclass = BaseParser.get_all_subclass()
        # 过滤掉禁用的平台
        enabled_classes = [
            _cls
            for _cls in all_subclass
            if _cls.platform.display_name in self.config["enable_platforms"]
        ]
        # 启用的平台
        platform_names = []
        for _cls in enabled_classes:
            parser = _cls(self.config, self.downloader)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser
        logger.info(f"启用平台: {'、'.join(platform_names)}")

        # 关键词-正则对，一次性生成并排序
        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in enabled_classes
            for kw, pt in cls._key_patterns
        ]
        # 长关键词优先
        patterns.sort(key=lambda x: -len(x[0]))
        keywords = [kw for kw, _ in patterns]
        logger.debug(f"关键词-正则对已生成：{keywords}")
        self.key_pattern_list = patterns

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type.__name__} 的 parser 实例，请检查是否已启用该平台")

    # endregion

    # region 核心逻辑

    def _build_send_plan(self, result: ParseResult):
        light_contents = []
        heavy_contents = []

        for cont in chain(
            result.contents, result.repost.contents if result.repost else ()
        ):
            match cont:
                case ImageContent() | GraphicsContent():
                    light_contents.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy_contents.append(cont)
                case _:
                    light_contents.append(cont)

        heavy_count = len(heavy_contents)
        light_count = len(light_contents)

        # 总消息条数 = 重媒体 + 轻媒体
        total_seg_count = heavy_count + light_count
        
        # 判断是否合并转发
        force_merge = total_seg_count >= self.config["forward_threshold"]

        return {
            "light": light_contents,
            "heavy": heavy_contents,
            "force_merge": force_merge,
        }

    async def _send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        plan = self._build_send_plan(result)

        segs: list[BaseMessageComponent] = []
        show_download_fail_tip = self.config.get("show_download_fail_tip", True)

        # 轻媒体
        for cont in plan["light"]:
            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("\n[图片下载失败]"))
                continue

            match cont:
                case ImageContent():
                    segs.append(Image(str(path)))
                case GraphicsContent() as g:
                    segs.append(Image(str(path)))
                    # 纯粹模式：不发送图文中的文字

        # 重媒体
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                if show_download_fail_tip:
                    segs.append(Plain("\n[超过文件大小限制]"))
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("\n[媒体下载失败]"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(Video(str(path)))
                case AudioContent():
                    segs.append(File(name=path.name, file=str(path)))
                case FileContent():
                    segs.append(File(name=path.name, file=str(path)))

        # 发送
        if not segs:
            return

        # 强制合并转发
        if plan["force_merge"]:
            nodes = Nodes([])
            self_id = event.get_self_id()
            
            # --- 关键修改：将每个组件单独封装为一个 Node ---
            # 这样在合并转发详情页里，每一张图片、每一个视频都会是独立的气泡
            # 既解决了图片粘连问题，也解决了文件显示问题
            
            for seg in segs:
                # 动态设置发送者名字，提升体验（可选）
                node_name = "解析结果"
                if isinstance(seg, File):
                    node_name = "媒体文件"
                elif isinstance(seg, Video):
                    node_name = "视频"
                elif isinstance(seg, Image):
                    node_name = "图片"
                
                nodes.nodes.append(Node(uin=self_id, name=node_name, content=[seg]))

            await event.send(event.chain_result([nodes]))
        else:
            await event.send(event.chain_result(segs))

    # endregion

    # region 事件监听

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 禁用会话
        if umo in self.config["disabled_sessions"]:
            return

        # 1. 获取消息文本 (处理 JSON 卡片)
        chain = event.get_messages()
        if not chain:
            return
        
        seg1 = chain[0]
        text = event.message_str
        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            logger.debug(f"解析Json组件: {text}")
        
        if not text:
            return

        # 2. 预检查：是指令吗？
        prefixes = self.context.get_config().get("command_prefixes", ["/"])
        is_command = any(text.strip().startswith(p) for p in prefixes)

        # 3. 预检查：包含链接吗？(如果不是指令才检查)
        keyword: str = ""
        searched: re.Match[str] | None = None
        if not is_command:
            # 指定机制：专门@其他bot的消息不解析
            self_id = event.get_self_id()
            if isinstance(seg1, At) and str(seg1.qq) != self_id:
                return

            for kw, pat in self.key_pattern_list:
                if kw not in text:
                    continue
                if m := pat.search(text):
                    keyword, searched = kw, m
                    break
        
        # 4. 关键判断：如果既不是指令，也没匹配到链接，直接退出！
        if not is_command and not searched:
            return

        # 5. 仲裁机制 (贴表情)
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                is_win = await self.arbiter.compete(
                    bot=event.bot,
                    ctx=ArbiterContext(
                        message_id=int(raw["message_id"]),
                        msg_time=int(raw["time"]),
                        self_id=int(raw["self_id"]),
                    ),
                )
                if not is_win:
                    logger.debug("Bot在仲裁中输了, 跳过解析")
                    return
                logger.debug("Bot在仲裁中胜出")

        # 6. 分流处理
        if is_command:
            return

        # 7. 解析逻辑
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 防抖机制
        link = searched.group(0)
        if self.config["debounce_interval"] and self.debouncer.hit(umo, link):
            logger.warning(f"[防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        try:
            parse_res = await self.parser_map[keyword].parse(keyword, searched)
            await self._send_parse_result(event, parse_res)
        except DurationLimitException as e:
            await event.send(event.plain_result(f"⚠️ {e}"))
        except SizeLimitException as e:
            await event.send(event.plain_result(f"⚠️ {e}"))
        except Exception:
            # 优化：使用 logger.exception 打印完整堆栈
            logger.exception("解析过程中发生未知错误")

    # endregion

    # region 指令

    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        if umo in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].remove(umo)
            self.config.save_config()
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        if umo not in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].append(umo)
            self.config.save_config()
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")

    # endregion