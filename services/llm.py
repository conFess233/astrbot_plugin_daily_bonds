"""插件自己的模型调用：provider 解析与回退、超时、并发与每群频率限制。

LLM 文案与好感度评估共用这一层。请求独立于主聊天：不带工具、不写会话历史、
不触发主聊天的 LLM 钩子。日志只记 provider、耗时与失败原因，不记提示词和输出。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from astrbot.api import logger

T = TypeVar("T")
_WINDOW_SECONDS = 60.0
# 调用失败的模型暂时排到最后：连续失败按 60、120、240… 秒递增，最长 10 分钟；
# 配额或限流错误（429）至少 5 分钟，避免每次都先等它重试、超时再换下一个。
_FAILURE_BASE_SECONDS = 60.0
_FAILURE_MAX_SECONDS = 600.0
_RATE_LIMIT_SECONDS = 300.0
_RATE_LIMIT_PATTERN = re.compile(
    r"429|RESOURCE_EXHAUSTED|rate.?limit|quota|too many requests", re.IGNORECASE
)
# 错误详情写日志前去掉可能出现的密钥，并截断长度。
_SECRET_PATTERN = re.compile(
    r"((?:api[_-]?)?key=|bearer\s+|authorization:\s*)[^\s&'\",]+|sk-[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)
_DETAIL_MAX_CHARS = 160


def _redacted(value: object) -> str:
    """压成一行、去掉可能出现的密钥并截断，用于把错误说明写进日志。"""

    text = " ".join(str(value).split())
    text = _SECRET_PATTERN.sub(lambda match: (match.group(1) or "") + "***", text)
    if len(text) > _DETAIL_MAX_CHARS:
        text = text[: _DETAIL_MAX_CHARS - 1] + "…"
    return text


def _error_detail(exc: BaseException) -> str:
    """异常类型加脱敏后的说明，便于从日志判断是限流、鉴权还是服务端错误。"""

    text = _redacted(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


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
        # 后台评估单独一个名额排队，不占聊天回复的并发。
        self._background = asyncio.Semaphore(1)
        self._closed = False
        # provider → (连续失败次数, 冷却截止时间)；成功一次即清除。
        self._failures: dict[str, tuple[int, float]] = {}

    def close(self) -> None:
        """停用时拒绝新调用并清空计数；在途调用由调用方取消。"""

        self._closed = True
        self._calls.clear()
        self._failures.clear()

    def has_quota(self, scope_id: int, per_minute: int) -> bool:
        """本群当前是否还有调用额度（不占用）。"""

        now = self._clock()
        calls = self._calls.get(scope_id)
        if calls is None:
            return True
        return sum(1 for item in calls if item > now - _WINDOW_SECONDS) < per_minute

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

    def _ordered(self, providers: list[str]) -> list[str]:
        """冷却中的模型排到最后，仍保留尝试机会：全部冷却时照原顺序依次尝试。"""

        now = self._clock()
        cooling = {
            provider
            for provider in providers
            if (state := self._failures.get(provider)) is not None and state[1] > now
        }
        if not cooling:
            return providers
        return [item for item in providers if item not in cooling] + [
            item for item in providers if item in cooling
        ]

    def _record_failure(self, provider_id: str, *, rate_limited: bool) -> float:
        count = self._failures.get(provider_id, (0, 0.0))[0] + 1
        seconds = min(_FAILURE_MAX_SECONDS, _FAILURE_BASE_SECONDS * 2 ** (count - 1))
        if rate_limited:
            seconds = max(seconds, _RATE_LIMIT_SECONDS)
        self._failures[provider_id] = (count, self._clock() + seconds)
        return seconds

    def _record_success(self, provider_id: str) -> None:
        self._failures.pop(provider_id, None)

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
        background: bool = False,
        timeout_seconds: float | None = None,
    ) -> T | None:
        """依次尝试 provider，返回第一个通过 accept 的结果；全部失败返回 None。

        accept 负责校验和清洗输出，返回 None 表示不合格，继续下一个 provider。
        聊天回复（background=False）共用 max_concurrency 个名额，已满时立即放弃、
        不排队；后台评估（background=True）排队使用自己的一个名额，不挤占回复。
        timeout_seconds 给后台任务单独的单次超时，总时长按 provider 个数放宽，
        不受聊天回复的超时限制。
        """

        if self._closed:
            return None
        limiter = (
            self._background
            if background
            else self._limiter(int(settings["max_concurrency"]))
        )
        if not background and limiter.locked():
            return None
        if not self.take_quota(scope_id, int(settings["group_calls_per_minute"])):
            return None
        async with limiter:
            if self._closed:
                return None
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            providers = self._ordered(await self._provider_ids(umo, settings))
            if timeout_seconds is None:
                per_call = float(settings["call_timeout_seconds"])
                deadline = started_at + float(settings["total_timeout_seconds"])
            else:
                per_call = float(timeout_seconds)
                deadline = started_at + per_call * max(1, len(providers))
            persona = await self._persona_prompt(umo, settings)
            system = "\n\n".join(
                part
                for part in (
                    f"以下人格设定只用于参考说话风格：\n{persona}" if persona else "",
                    system_prompt,
                )
                if part
            )
            for provider_id in providers:
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
                        timeout=min(per_call, remaining),
                    )
                except TimeoutError:
                    cooldown = self._record_failure(provider_id, rate_limited=False)
                    logger.warning(
                        "今日姻缘：模型 %s 调用超时（%.0f 秒），%.0f 秒内排到最后，尝试下一个。",
                        provider_id,
                        loop.time() - started,
                        cooldown,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - provider 边界：换下一个
                    detail = _error_detail(exc)
                    cooldown = self._record_failure(
                        provider_id,
                        rate_limited=bool(_RATE_LIMIT_PATTERN.search(detail)),
                    )
                    logger.warning(
                        "今日姻缘：模型 %s 调用失败，原因=%s；%.0f 秒内排到最后。",
                        provider_id,
                        detail,
                        cooldown,
                    )
                    continue
                if getattr(response, "role", "assistant") == "err":
                    detail = _redacted(getattr(response, "completion_text", "") or "")
                    cooldown = self._record_failure(
                        provider_id,
                        rate_limited=bool(_RATE_LIMIT_PATTERN.search(detail)),
                    )
                    logger.warning(
                        "今日姻缘：模型 %s 返回错误（%s），%.0f 秒内排到最后。",
                        provider_id,
                        detail or "无详情",
                        cooldown,
                    )
                    continue
                # 能正常返回就说明模型可用，输出不合格不算调用失败。
                self._record_success(provider_id)
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
