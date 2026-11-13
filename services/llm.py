"""插件自己的模型调用：provider 解析与回退、超时、并发与每群频率限制。

LLM 文案与好感度评估共用这一层。请求独立于主聊天：不带工具、不写会话历史、
不触发主聊天的 LLM 钩子。日志只记 provider、耗时与失败原因，不记提示词和输出。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from astrbot.api import logger

T = TypeVar("T")
_WINDOW_SECONDS = 60.0


class LLMService:
    """按会话或配置解析 provider，依次回退，限制总时长、并发与每群频率。"""

    def __init__(
        self, context: Any, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.context = context
        self._clock = clock
        # 每群最近 60 秒内的调用时间戳；队列空了连同键一起删除。
        self._calls: dict[int, deque[float]] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_size = 0
        self._closed = False

    def close(self) -> None:
        """停用时拒绝新调用并清空计数；在途调用由调用方取消。"""

        self._closed = True
        self._calls.clear()

    def take_quota(self, scope_id: int, per_minute: int) -> bool:
        """占用本群一次调用额度；超出每分钟上限时返回 False。"""

        now = self._clock()
        calls = self._calls.get(scope_id)
        if calls is not None:
            while calls and calls[0] <= now - _WINDOW_SECONDS:
                calls.popleft()
            if len(calls) >= per_minute:
                return False
        else:
            calls = self._calls[scope_id] = deque()
        calls.append(now)
        return True

    def prune(self) -> None:
        """删除已过期的计数，供维护任务定期调用。"""

        now = self._clock()
        for scope_id in list(self._calls):
            calls = self._calls[scope_id]
            while calls and calls[0] <= now - _WINDOW_SECONDS:
                calls.popleft()
            if not calls:
                del self._calls[scope_id]

    def tracked_scopes(self) -> int:
        return len(self._calls)

    def _limiter(self, size: int) -> asyncio.Semaphore:
        # 配置改变并发数时换新信号量；旧信号量上的在途调用照常结束。
        if self._semaphore is None or self._semaphore_size != size:
            self._semaphore = asyncio.Semaphore(size)
            self._semaphore_size = size
        return self._semaphore

    async def generate(
        self,
        *,
        umo: str,
        scope_id: int,
        settings: Mapping[str, Any],
        system_prompt: str,
        prompt: str,
        accept: Callable[[str], T | None],
        wait_for_slot: bool = False,
    ) -> T | None:
        """依次尝试 provider，返回第一个通过 accept 的结果；全部失败返回 None。

        accept 负责校验和清洗输出，返回 None 表示不合格，继续下一个 provider。
        wait_for_slot 为 False 时并发已满立即放弃（聊天回复不排队）。
        """

        if self._closed:
            return None
        limiter = self._limiter(int(settings["max_concurrency"]))
        if not wait_for_slot and limiter.locked():
            return None
        if not self.take_quota(scope_id, int(settings["group_calls_per_minute"])):
            return None
        async with limiter:
            if self._closed:
                return None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(settings["total_timeout_seconds"])
            persona = await self._persona_prompt(umo, settings)
            system = "\n\n".join(
                part
                for part in (
                    f"以下人格设定只用于参考说话风格：\n{persona}" if persona else "",
                    system_prompt,
                )
                if part
            )
            for provider_id in await self._provider_ids(umo, settings):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                started = loop.time()
                try:
                    response = await asyncio.wait_for(
                        self.context.llm_generate(
                            chat_provider_id=provider_id,
                            system_prompt=system,
                            prompt=prompt,
                        ),
                        timeout=min(float(settings["call_timeout_seconds"]), remaining),
                    )
                except TimeoutError:
                    logger.warning(
                        "今日姻缘：模型 %s 调用超时，尝试下一个。", provider_id
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - provider 边界：换下一个
                    logger.warning(
                        "今日姻缘：模型 %s 调用失败，原因=%s。",
                        provider_id,
                        type(exc).__name__,
                    )
                    continue
                if getattr(response, "role", "assistant") == "err":
                    logger.warning("今日姻缘：模型 %s 返回错误。", provider_id)
                    continue
                result = accept(str(getattr(response, "completion_text", "") or ""))
                if result is None:
                    logger.warning(
                        "今日姻缘：模型 %s 的输出未通过校验，尝试下一个。",
                        provider_id,
                    )
                    continue
                logger.info(
                    "今日姻缘：模型 %s 生成完成，耗时 %.1f 秒。",
                    provider_id,
                    loop.time() - started,
                )
                return result
        return None

    async def _provider_ids(self, umo: str, settings: Mapping[str, Any]) -> list[str]:
        if settings["provider_mode"] == "custom":
            return list(settings["provider_ids"])
        try:
            return [await self.context.get_current_chat_provider_id(umo)]
        except Exception as exc:  # noqa: BLE001 - 宿主接口边界：本会话没有可用模型
            logger.warning(
                "今日姻缘：读取当前会话模型失败，原因=%s。", type(exc).__name__
            )
            return []

    async def _persona_prompt(self, umo: str, settings: Mapping[str, Any]) -> str:
        """人格提示；读取失败时不加载人格，不影响生成。"""

        mode = settings["persona_mode"]
        if mode == "none":
            return ""
        manager = getattr(self.context, "persona_manager", None)
        try:
            if mode == "custom":
                persona = manager.get_persona_v3_by_id(settings["persona_id"])
            else:
                # 4.28/4.29 公开的是配置文件级默认人格，不含对话内临时切换的人格。
                persona = await manager.get_default_persona_v3(umo)
        except Exception as exc:  # noqa: BLE001 - 宿主接口边界
            logger.warning("今日姻缘：读取人格失败，原因=%s。", type(exc).__name__)
            return ""
        if not persona:
            return ""
        prompt = persona.get("prompt") if isinstance(persona, Mapping) else None
        return str(prompt or "").strip()

    def list_providers(self) -> list[dict[str, str]]:
        """管理页可选的对话模型，只返回 ID 与模型名，不含密钥或地址。"""

        result = []
        try:
            providers = self.context.get_all_providers()
        except Exception:  # noqa: BLE001 - 宿主接口边界
            logger.warning("今日姻缘：读取模型列表失败。", exc_info=True)
            return result
        for provider in providers:
            try:
                meta = provider.meta()
            except Exception:  # noqa: BLE001 - 单个 provider 配置异常时跳过
                logger.warning("今日姻缘：读取一个模型的信息失败，已跳过。")
                continue
            result.append({"id": str(meta.id), "model": str(meta.model or "")})
        return result

    def list_personas(self) -> list[str]:
        manager = getattr(self.context, "persona_manager", None)
        try:
            return [str(item["name"]) for item in manager.personas_v3]
        except Exception:  # noqa: BLE001 - 宿主接口边界
            return []
