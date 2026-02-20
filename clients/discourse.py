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

    async def find_user_blog(self, username: str) -> dict | None:
        """Найти подкатегорию блога пользователя.

        Стратегии (в порядке приоритета):
        0. Прямой доступ: GET /c/blogs/{slug}.json (самый надёжный)
        1. Список подкатегорий + matching по slug/name/user_id
        """
        # Strategy 0: Direct access by slug (не зависит от листинга)
        blog = await self._find_blog_direct(username)
        if blog:
            return blog

        # Strategy 1: List subcategories + matching
        subcategories = await self._list_blog_subcategories()
        username_lower = username.lower()

        for cat in subcategories:
            slug = cat.get("slug", "").lower()
            name = cat.get("name", "").lower()
            if slug == username_lower or username_lower in slug or username_lower in name:
                logger.info(f"Blog found by list match: {cat.get('slug')} (id={cat.get('id')})")
                return cat

        # Strategy 2: blogs-user-{discourse_id} pattern
        user = await self.get_user(username)
        if user and user.get("id"):
            user_id = user["id"]
            for cat in subcategories:
                if cat.get("slug", "").lower() == f"blogs-user-{user_id}":
                    logger.info(f"Blog found by user_id pattern: {cat.get('slug')} (id={cat.get('id')})")
                    return cat
            # Direct access with blogs-user-{id} slug
            blog = await self._find_blog_direct(f"blogs-user-{user_id}")
            if blog:
                return blog

        logger.warning(
            f"Blog not found for '{username}'. "
            f"List returned {len(subcategories)} subcategories: "
            f"{[c.get('slug') for c in subcategories[:10]]}"
        )
        return None

    async def _find_blog_direct(self, slug: str) -> dict | None:
        """Прямой доступ к блогу: GET /c/blogs/{slug}.json.

        Discourse при доступе к категории по slug может:
        - вернуть 200 с topic_list (ID извлекаем из URL или topics)
        - вернуть 301 → /c/blogs/{slug}/{id}.json (ID в URL)
        - вернуть 404 (блог не существует)
        """
        session = await self._get_session()
        slug = slug.lower()
        url = f"{self.base_url}/c/blogs/{slug}.json"

        try:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    return None

                # 1. Извлечь ID из финального URL (после редиректа)
                # Pattern: /c/blogs/{slug}/{id}.json
                final_path = str(resp.url.path) if hasattr(resp.url, 'path') else str(resp.url)
                m = re.search(r'/(\d+)\.json$', final_path)
                if m:
                    cat_id = int(m.group(1))
                    logger.info(f"Blog found by direct URL: {slug} (id={cat_id})")
                    return {"id": cat_id, "slug": slug}

                # 2. Извлечь из первого топика в ответе
                data = await resp.json()
                topics = data.get("topic_list", {}).get("topics", [])
                if topics:
                    cat_id = topics[0].get("category_id")
                    if cat_id:
                        logger.info(f"Blog found by topic category_id: {slug} (id={cat_id})")
                        return {"id": cat_id, "slug": slug}

                # 3. Извлечь из per_page metadata
                per_page = data.get("topic_list", {}).get("per_page")
                if per_page:
                    # Категория существует, но пуста. Попробуем /c/{parent}/show.json
                    cat = await self._find_subcategory_id_by_slug(slug)
                    if cat:
                        return cat

                logger.warning(f"Blog /c/blogs/{slug} accessible but couldn't extract ID")
        except Exception as e:
            logger.debug(f"Direct blog access error for {slug}: {e}")

        return None

    async def _find_subcategory_id_by_slug(self, slug: str) -> dict | None:
        """Найти ID подкатегории через /c/{parent}/show.json → subcategory_ids."""
        session = await self._get_session()
        url = f"{self.base_url}/c/{self.blogs_category_id}/show.json"
        try:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                sub_ids = data.get("category", {}).get("subcategory_ids", [])
                for sid in sub_ids:
                    cat_url = f"{self.base_url}/c/{sid}/show.json"
                    async with session.get(cat_url, headers=self._headers()) as cat_resp:
                        if cat_resp.status == 200:
                            cat_data = await cat_resp.json()
                            cat = cat_data.get("category", {})
                            if cat.get("slug", "").lower() == slug.lower():
                                logger.info(f"Blog found by show.json lookup: {slug} (id={sid})")
                                return cat
        except Exception as e:
            logger.debug(f"subcategory_id lookup error: {e}")
        return None

    async def _list_blog_subcategories(self) -> list[dict]:
        """Список подкатегорий /c/blogs/ через /categories.json и /site.json."""
        session = await self._get_session()

        # Strategy A: /categories.json?include_subcategories=true
        try:
            url = f"{self.base_url}/categories.json"
            async with session.get(
                url,
                headers=self._headers(),
                params={"include_subcategories": "true"},
            ) as resp:
                if resp.status < 400:
                    data = await resp.json()
                    cats = data.get("category_list", {}).get("categories", [])

                    # Check nested subcategory_list in parent
                    for cat in cats:
                        if cat.get("id") == self.blogs_category_id:
                            subs = cat.get("subcategory_list", [])
                            if subs:
                                logger.info(f"Discourse: {len(subs)} subcats via nested list")
                                return subs
                            break

                    # Check flat list with parent_category_id
                    result = [c for c in cats if c.get("parent_category_id") == self.blogs_category_id]
                    if result:
                        logger.info(f"Discourse: {len(result)} subcats via flat list filter")
                        return result
        except Exception as e:
            logger.warning(f"/categories.json error: {e}")

        # Strategy B: /site.json
        try:
            url = f"{self.base_url}/site.json"
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status < 400:
                    data = await resp.json()
                    categories = data.get("categories", [])
                    result = [c for c in categories if c.get("parent_category_id") == self.blogs_category_id]
                    logger.info(f"Discourse: {len(result)} subcats via /site.json (total: {len(categories)})")
                    return result
        except Exception as e:
            logger.error(f"/site.json error: {e}")

        return []

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
