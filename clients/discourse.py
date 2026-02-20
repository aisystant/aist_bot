"""
Discourse API клиент — публикация постов на systemsworld.club.

Использует Api-Key + Api-Username для публикации от имени пользователей.
Scopes: topics (write, read, read_lists), uploads (create), categories (list, show), users (show).

Использование:
    from clients.discourse import discourse

    # Проверить пользователя
    user = await discourse.get_user("tseren-tserenov")

    # Найти блог пользователя
    cat = await discourse.find_user_blog("tseren-tserenov")

    # Опубликовать пост
    topic = await discourse.create_topic(
        category_id=37,
        title="Мой пост",
        raw="Контент в **Markdown**.",
        username="tseren-tserenov",
    )
"""

import re
import aiohttp
from config import get_logger

logger = get_logger(__name__)


class DiscourseClient:
    """HTTP-клиент для Discourse REST API."""

    _session: aiohttp.ClientSession | None = None

    def __init__(self, base_url: str, api_key: str, blogs_category_id: int = 36):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.blogs_category_id = blogs_category_id

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    def _headers(self, username: str = "system") -> dict:
        return {
            "Api-Key": self.api_key,
            "Api-Username": username,
            "Content-Type": "application/json",
        }

    # ── Users ──────────────────────────────────────────────

    async def get_user(self, username: str) -> dict | None:
        """Проверить, существует ли пользователь. Возвращает user dict или None."""
        session = await self._get_session()
        url = f"{self.base_url}/users/{username}.json"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status == 404:
                return None
            if resp.status >= 400:
                text = await resp.text()
                logger.error(f"Discourse get_user error {resp.status}: {text}")
                return None
            data = await resp.json()
            return data.get("user")

    # ── Categories ─────────────────────────────────────────

    async def get_category(self, category_id: int) -> dict | None:
        """Получить категорию по ID (scope: categories:show). 1 запрос."""
        session = await self._get_session()
        url = f"{self.base_url}/c/{category_id}/show.json"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                logger.error(f"Discourse get_category {category_id} error {resp.status}")
                return None
            data = await resp.json()
            return data.get("category")

    async def find_user_blog(self, username: str) -> dict | None:
        """Найти подкатегорию блога пользователя.

        Discourse slugs блогов: blogs-user-{discourse_user_id} (НЕ username!).
        URL вида /c/blogs/tseren-tserenov/37 работает в браузере только потому,
        что Discourse ищет по ID (37), а slug в URL — декоративный.

        Стратегия: GET /site.json → найти категорию по slug blogs-user-{id}
        или по name, содержащему username.
        """
        # 1. Получить Discourse user ID
        user = await self.get_user(username)
        discourse_user_id = user.get("id") if user else None
        logger.info(
            f"find_user_blog: username={username}, "
            f"discourse_user_id={discourse_user_id}, "
            f"blogs_parent_category_id={self.blogs_category_id}"
        )

        # 2. Искать через /categories.json (scope: categories:list)
        session = await self._get_session()
        try:
            # Запрос подкатегорий блогов напрямую (parent_category_id фильтрует)
            all_cats = []
            for page in range(5):  # max 5 страниц (≈150 категорий)
                url = (
                    f"{self.base_url}/categories.json"
                    f"?parent_category_id={self.blogs_category_id}&page={page}"
                )
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status >= 400:
                        logger.error(f"/categories.json page={page} returned {resp.status}")
                        break
                    data = await resp.json()
                cats_page = data.get("category_list", {}).get("categories", [])
                if not cats_page:
                    break
                all_cats.extend(cats_page)
                logger.info(
                    f"find_user_blog: page {page}: {len(cats_page)} categories"
                )
                if len(cats_page) < 30:  # последняя страница
                    break
            # all_cats уже отфильтрованы по parent_category_id
            blog_children = all_cats
            logger.info(
                f"find_user_blog: {len(blog_children)} blog subcategories found"
            )
            if blog_children:
                logger.info(
                    f"find_user_blog: first 10 slugs = "
                    f"{[c.get('slug') for c in blog_children[:10]]}"
                )

            username_lower = username.lower()
            target_slug = f"blogs-user-{discourse_user_id}" if discourse_user_id else None
            # Нормализация: "tseren-tserenov" → "tseren tserenov"
            username_norm = re.sub(r"[-_.]", " ", username_lower)

            # Strategy 1: children of configured parent — slug match
            for cat in blog_children:
                slug = cat.get("slug", "").lower()
                if target_slug and slug == target_slug:
                    logger.info(
                        f"Blog found by parent+slug: {slug} "
                        f"(id={cat.get('id')}, user_id={discourse_user_id})"
                    )
                    return cat

            # Strategy 2: children of configured parent — name match
            for cat in blog_children:
                name = cat.get("name", "").lower()
                name_norm = re.sub(r"[-_.]", " ", name)
                if username_norm in name_norm:
                    logger.info(
                        f"Blog found by parent+name: {cat.get('name')} "
                        f"(id={cat.get('id')}, slug={cat.get('slug')})"
                    )
                    return cat

            logger.warning(
                f"No match among {len(blog_children)} subcategories "
                f"(target_slug={target_slug}, username_norm={username_norm!r})"
            )

        except Exception as e:
            logger.error(f"find_user_blog /categories.json error: {e}")

        logger.warning(
            f"Blog not found for '{username}' "
            f"(user_id={discourse_user_id}, "
            f"blogs_parent={self.blogs_category_id})"
        )
        return None

    # ── Topics ─────────────────────────────────────────────

    async def create_topic(
        self,
        category_id: int,
        title: str,
        raw: str,
        username: str,
        tags: list[str] | None = None,
    ) -> dict:
        """Создать топик (пост) в категории от имени пользователя."""
        session = await self._get_session()
        payload: dict = {
            "title": title,
            "raw": raw,
            "category": category_id,
        }
        if tags:
            payload["tags"] = tags

        logger.info(
            f"Discourse create_topic: category={category_id}, "
            f"user={username}, title={title[:50]!r}"
        )

        url = f"{self.base_url}/posts.json"
        async with session.post(url, json=payload, headers=self._headers(username)) as resp:
            try:
                data = await resp.json()
            except Exception:
                text = await resp.text()
                logger.error(
                    f"Discourse create_topic non-JSON {resp.status}: {text[:500]}"
                )
                raise DiscourseError(f"HTTP {resp.status}: ответ не JSON")

            if resp.status >= 400:
                errors = data.get("errors", [])
                error_type = data.get("error_type", "unknown")
                error_msg = "; ".join(errors) if errors else str(data)
                logger.error(
                    f"Discourse create_topic error {resp.status} ({error_type}): "
                    f"{error_msg} [category={category_id}, user={username}]"
                )
                raise DiscourseError(
                    f"HTTP {resp.status} ({error_type}): {error_msg}"
                )
            logger.info(
                f"Discourse topic created: id={data.get('topic_id')}, "
                f"user={username}, category={category_id}"
            )
            return data

    async def list_category_topics(self, category_id: int, slug: str = "") -> list[dict]:
        """Список топиков в категории."""
        session = await self._get_session()
        if slug:
            url = f"{self.base_url}/c/{slug}/{category_id}.json"
        else:
            url = f"{self.base_url}/c/{category_id}.json"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                logger.error(f"Discourse list_category_topics error {resp.status}")
                return []
            data = await resp.json()
            return data.get("topic_list", {}).get("topics", [])

    async def get_topic(self, topic_id: int) -> dict | None:
        """Получить топик с постами (для мониторинга комментариев)."""
        session = await self._get_session()
        url = f"{self.base_url}/t/{topic_id}.json"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                logger.error(f"Discourse get_topic error {resp.status}")
                return None
            return await resp.json()

    # ── Cleanup ────────────────────────────────────────────

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class DiscourseError(Exception):
    """Ошибка Discourse API."""
    pass


# ── Singleton ──────────────────────────────────────────────

import os

_discourse_url = os.getenv("DISCOURSE_API_URL", "")
_discourse_key = os.getenv("DISCOURSE_API_KEY", "")
_blogs_cat_id = int(os.getenv("DISCOURSE_BLOGS_CATEGORY_ID", "36"))

discourse = DiscourseClient(_discourse_url, _discourse_key, _blogs_cat_id) if _discourse_url else None
