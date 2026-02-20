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

    async def list_blog_subcategories(self) -> list[dict]:
        """Список подкатегорий /c/blogs/ (блоги пользователей)."""
        session = await self._get_session()
        url = f"{self.base_url}/c/{self.blogs_category_id}/show.json"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                logger.error(f"Discourse list_blog_subcategories error {resp.status}")
                return []
            data = await resp.json()
            cat = data.get("category", {})
            # Subcategory list may be embedded or referenced by IDs
            subcategory_list = cat.get("subcategory_list", [])
            if subcategory_list:
                return subcategory_list
            # Fallback: fetch via site.json
            return await self._fetch_subcategories_via_site()

    async def _fetch_subcategories_via_site(self) -> list[dict]:
        """Fallback: получить подкатегории через /site.json."""
        session = await self._get_session()
        url = f"{self.base_url}/site.json"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                return []
            data = await resp.json()
            categories = data.get("categories", [])
            return [
                c for c in categories
                if c.get("parent_category_id") == self.blogs_category_id
            ]

    async def find_user_blog(self, username: str) -> dict | None:
        """Найти подкатегорию блога пользователя по username/slug."""
        subcategories = await self.list_blog_subcategories()
        username_lower = username.lower()
        for cat in subcategories:
            slug = cat.get("slug", "").lower()
            name = cat.get("name", "").lower()
            if username_lower in slug or username_lower in name:
                return cat
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

        url = f"{self.base_url}/posts.json"
        async with session.post(url, json=payload, headers=self._headers(username)) as resp:
            data = await resp.json()
            if resp.status >= 400:
                errors = data.get("errors", [])
                error_msg = "; ".join(errors) if errors else str(data)
                logger.error(f"Discourse create_topic error {resp.status}: {error_msg}")
                raise DiscourseError(f"Ошибка публикации: {error_msg}")
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
