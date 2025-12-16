import asyncio
import random
import time
from collections import defaultdict
from typing import TypedDict

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig


class EmojiLikeRecord(TypedDict):
    """一次 emoji_like 行为"""

    user_id: int  # 谁贴的（仅用于内部日志）
    emoji_id: int  # 表情编号
    ts: float  # 事件时间


class EmojiLikeArbiter:
    """
    group_msg_emoji_like 延迟仲裁器

    对外返回：
        True  -> 自己胜出
        False -> 未参与 / 失败
    """

    def __init__(self, config: AstrBotConfig):
        self.wait_sec = config["arbiter_wait_sec"]

        # message_id -> 所有 emoji_like 记录
        self._like_cache: dict[int, list[EmojiLikeRecord]] = defaultdict(list)

        # message_id -> 自己贴的 emoji_id
        self._self_emoji: dict[int, int] = {}

        # 已裁决 message_id
        self._locks: set[int] = set()

    def record_like(self, raw_message: dict) -> None:
        """
        接收并解析 group_msg_emoji_like raw_message
        """
        if raw_message.get("notice_type") != "group_msg_emoji_like":
            return

        try:
            message_id = int(raw_message["message_id"])
            user_id = int(raw_message["user_id"])
            event_time = float(raw_message["time"])
            likes = raw_message.get("likes") or []
        except (KeyError, TypeError, ValueError):
            return

        for item in likes:
            try:
                emoji_id = int(item["emoji_id"])
            except (KeyError, TypeError, ValueError):
                continue

            self._like_cache[message_id].append(
                {
                    "user_id": user_id,
                    "emoji_id": emoji_id,
                    "ts": event_time,
                }
            )
            logger.debug(
                f"贴表情监听：用户({user_id})给消息({message_id})贴了表情({emoji_id})"
            )

    async def compete(
        self,
        bot,
        *,
        message_id: int,
        self_id: int,
    ) -> bool:
        """
        参与 emoji 竞选

        :return: 是否胜出
        """
        # 已裁决，直接退出
        if message_id in self._locks:
            return False

        # 贴表情前已经监听到有人贴过，放弃竞选
        if self._like_cache.get(message_id):
            return False

        emoji_id = random.randint(1, 433)

        # 尝试贴表情
        try:
            await bot.set_msg_emoji_like(
                message_id=message_id,
                emoji_id=emoji_id,
                set=True,
            )
        except Exception:
            return False

        # 贴成功后才记录自己
        self._self_emoji[message_id] = emoji_id
        self._like_cache[message_id].append(
            {
                "user_id": self_id,
                "emoji_id": emoji_id,
                "ts": time.time(),  # 本地兜底时间
            }
        )

        # 等待其他 bot / 用户参与
        await asyncio.sleep(self.wait_sec)

        return self._decide(message_id, self_id)


    def _decide(self, message_id: int, self_id: int) -> bool:
        """
        根据 emoji_id 最小值裁决胜负
        """
        if message_id in self._locks:
            return False

        records = self._like_cache.get(message_id)
        my_emoji = self._self_emoji.get(message_id)

        try:
            if not records or my_emoji is None:
                return False

            winner = min(records, key=lambda r: r["emoji_id"])

            logger.debug(
                f"链接消息（{message_id}）的仲裁赢家为：{winner['user_id']}。表情ID：{winner['emoji_id']}",
            )

            self._locks.add(message_id)

            return winner["user_id"] == self_id
        finally:
            # 清理状态
            self._like_cache.pop(message_id, None)
            self._self_emoji.pop(message_id, None)
