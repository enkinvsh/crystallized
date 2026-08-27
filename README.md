# <img src="docs/icons/brain.svg" width="28" alt="" /> Crystallized

<a href="https://web.tribute.tg/d/Huc">
  <img src="https://img.shields.io/badge/Sponsor-ea4aaa?style=for-the-badge&logo=github-sponsors&logoColor=white" alt="Sponsor">
</a>

<br><br>

У каждой языковой модели судьба Леонарда Шелби из фильма «Помни» (Memento).

Каждое утро ты просыпаешься, и последние воспоминания стерты. Ты не помнишь, кто твой друг, кому нельзя верить и над чем ты работал вчера. Единственное, на что может опереться Леонард, — это записки в кармане и татуировки на собственном теле.

Обычный ИИ-ассистент живет точно так же. Закрыл терминал — наступила амнезия. Ты снова объясняешь ему свой стек, просишь не ломать конфиг и одергиваешь от одних и тех же ошибок.

Обычно эту проблему пытаются решить «свалкой полароидов» (векторным RAG). В итоге модель достает случайную старую записку трехмесячной давности, где написано ровно противоположное тому, что ты просил вчера, и снова путается.

**Crystallized — это живая система татуировок и записок, которая сама наводит порядок.**

---

## <img src="docs/icons/workflow.svg" width="22" alt="" /> Как это устроено

Вместо того чтобы хранить терабайты бессмысленного чата, система превращает живой опыт в жесткие привычки:

```
1. ДЕНЬ (Живой опыт и отказы)
   Ты отменил действие или написал: «Не трогай этот файл».
   Наблюдатель за 0.05мс делает быструю пометку в блокноте.

2. ВЕЧЕР (Сессия закончена)
   Все правки дня связываются в историю:
   «Попробовал вариант А -> человеку не подошло из-за Б -> сделали В».

3. НОЧЬ (Фаза сна в 04:00)
   Пока все спят, система переваривает весь опыт.
   Мелкий мусор стирается, а важное знание становится постоянной татуировкой.

4. УТРО (Новая сессия)
   Модель просыпается с чистого листа, но УЖЕ видит ключевые правила
   и твои привычки с самой первой секунды.
```

### Главные правила:
- **Свежая татуировка вытесняет старую**: если ты сменил решение и перешел на другой инструмент, старое правило не висит рядом. Оно тихо зачеркивается и уступает место новому.
- **Все на одном компьютере**: никаких серверов, облаков и баз данных Redis. Один легкий локальный файл SQLite, который летает за миллисекунды.
- **Честное затухание**: то, с чем ты работаешь каждый день, звучит громко. Случайные одноразовые мелочи со временем плавно выцветают.

---

## <img src="docs/icons/flash.svg" width="22" alt="" /> Установка в 1 клик через агента

Если ты работаешь в **OpenCode**, **Claude Code**, **Cursor** или **Windsurf**, просто скопируй этот промпт и отправь своему агенту:

```markdown
Установи и настрой память Crystallized из репозитория https://github.com/enkinvsh/crystallized.
Склонируй репозиторий, выполни ./install.sh, извлеки токен авторизации Claude Desktop
через python3 auth/extract_token.py и убедись, что MCP-сервер памяти подключен в opencode.json.
```

### Ручная установка через терминал:

```bash
git clone https://github.com/enkinvsh/crystallized.git
cd crystallized
./install.sh
```

---

## <img src="docs/icons/key.svg" width="22" alt="" /> Подписка Claude Desktop без переплат

Если на компьютере стоит официальное приложение **Claude Desktop** с подпиской Pro или Max, Crystallized сам заберет оттуда ключ:

- На **Mac**: `python3 auth/extract_token.py`
- На **Windows**: `python auth\extract_token.py`

Токены остаются только на твоем компьютере в `~/.local/share/opencode/auth.json`.

---

## <img src="docs/icons/workflow.svg" width="22" alt="" /> Модели через cliproxyapi

Локальный прокси поднимает OpenAI-совместимый эндпоинт на `127.0.0.1:8317`, и opencode ходит в него как в обычного провайдера.

```bash
brew install cliproxyapi
cliproxyapi -antigravity-login    # ещё есть -codex-login, -claude-login, -kimi-login, -xai-login
brew services start cliproxyapi
```

Логин кладет OAuth-креды в `auth-dir` из `/opt/homebrew/etc/cliproxyapi.conf` (по умолчанию `~/.cli-proxy-api`). Там же в `api-keys` задается свой локальный ключ — он и идет в `apiKey` ниже.

Фрагмент для `~/.config/opencode/opencode.json`:

```json
{
  "provider": {
    "codex": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CliproxyAPI",
      "options": {
        "baseURL": "http://127.0.0.1:8317/v1",
        "apiKey": "YOUR_LOCAL_API_KEY"
      },
      "models": {
        "gemini-3.7-flash-high": {
          "name": "Gemini 3.7 Flash (high)",
          "limit": { "context": 1048576, "output": 65536 },
          "attachment": true,
          "tool_call": true,
          "modalities": { "input": ["text", "image", "pdf"], "output": ["text"] }
        }
      }
    }
  }
}
```

Какие модели доступны — спроси у прокси и вписывай из этого списка:

```bash
curl -s -H "Authorization: Bearer YOUR_LOCAL_API_KEY" http://127.0.0.1:8317/v1/models
```

Если модель отвечает `User location is not supported` — сначала обнови прокси и перезапусти его, дай пару минут прогреться, и только потом ищи причину в сети.

---

## <img src="docs/icons/laptop.svg" width="22" alt="" /> Переезд на новый компьютер

Купил новый ноутбук? Скопируй свой файл `memory.db` и папку заметок `notes/`. Запусти установщик — и твой агент проснется со всеми своими накопленными татуировками и характером.

---

## <img src="docs/icons/heart.svg" width="22" alt="" /> Поддержать проект

- **TON / USDT (TON)**: `UQAhzOYPIBQthqrwCFcIpsWUZjmi4KrK3BuGnjAQmJW04IC8`
- **USDT (TRC-20)**: `TGHVN7y5EhZ4pAreAXbffKY86kpq1dve9h`
- **Карты**: [Tribute](https://web.tribute.tg/d/Huc)

---

## <img src="docs/icons/license.svg" width="22" alt="" /> Лицензия

MIT License.
