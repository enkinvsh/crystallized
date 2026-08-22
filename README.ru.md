# <img src="docs/icons/brain.svg" width="28" alt="" /> Crystallized

<a href="#поддержать-проект">
  <img src="https://img.shields.io/badge/Sponsor-ea4aaa?style=for-the-badge&logo=github-sponsors&logoColor=white" alt="Sponsor">
</a>

<br><br>

Детерминированная система долговременной памяти и би-темпорального управления состоянием убеждений для ИИ-агентов (OpenCode, Claude Code, Cursor).

Проблема стандартных сессий — эфемеренность контекста: модель начинает каждый запуск с нуля, забывая отрицательные ограничения, архитектурные инварианты и предпочтения по кодовой базе. Векторный RAG на истории диалогов приводит к семантическому дрейфу и смешиванию устаревших правил с актуальными.

Crystallized реализует модель иерархической причинно-следственной памяти с явным версионированием и атомарным вытеснением устаревших инструкций.

---

## <img src="docs/icons/workflow.svg" width="22" alt="" /> Архитектура и уровни сжатия

Консолидация опыта разделена на 4 слоя:

```
[ L0: Сырой след ] ──────► Перехват отказов и правок (<0.05мс, SQLite WAL)
        │
        ▼
[ L1: Эпизод ]     ──────► Склейка микро-коррекций в причинно-следственные цепочки (Session End)
        │
        ▼
[ L2: Паттерн ]    ──────► Дедупликация и фильтрация шума (Dream Daemon @ 04:00)
        │
        ▼
[ L3: Аксиома ]    ──────► Реестр активных убеждений (Belief State) -> Авто-инъекция в промпт
```

### Ключевые механизмы:
- **Явное вытеснение (Bi-Temporal Supersession)**: При изменении требования старое правило переводится в статус `superseded` с фиксацией временного интервала (`valid_to`). Активным всегда остается ровно одно правило на пару `(subject, predicate)`.
- **Затухание по степенному закону (Power-law Decay)**: Актуальность фактов пересчитывается по формуле затухания, отсекая неиспользуемый контекст.
- **Zero-Infrastructure**: Работает локально через один файл базы данных SQLite в режиме WAL (`memory.db`) и markdown-заметки. Никаких внешних сервисов и Redis.

---

## <img src="docs/icons/flash.svg" width="22" alt="" /> Установка

### Через агента (OpenCode / Claude Code / Cursor):

```markdown
Установи память Crystallized из репозитория https://github.com/enkinvsh/crystallized.
Выполни ./install.sh и убедись, что хуки и MCP-сервер зарегистрированы в конфигурации.
```

### Вручную:

```bash
git clone https://github.com/enkinvsh/crystallized.git
cd crystallized
./install.sh
```

---

## <img src="docs/icons/key.svg" width="22" alt="" /> Извлечение авторизации Claude Desktop

Для работы через подписку Claude Pro/Max без использования платного API-ключа предусмотрен экстрактор локальных токенов:

- **macOS**: `python3 auth/extract_token.py` (извлечение из macOS Keychain)
- **Windows**: `python auth\extract_token.py` (DPAPI дешифрование)

Токены сохраняются локально в `~/.local/share/opencode/auth.json`. Подробнее: [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md).

---

## <img src="docs/icons/tools.svg" width="22" alt="" /> MCP-инструменты

- `memory_belief_assert`: атомарная фиксация активного убеждения с вытеснением предшественника.
- `memory_belief_get_active`: чтение текущего правила по субъекту и предикату.
- `memory_causal_log`: запись причинно-следственной цепочки (триггер -> действие -> результат).
- `memory_save_fact` / `memory_get_fact`: хранение точных фактов с поддержкой TTL.
- `memory_save_doc` / `memory_read_doc`: управление структурированной документацией.
- `memory_recall`: гибридный поиск по базе фактов, векторов и документов.

Каталог дополнительных MCP-серверов (Serena, Playwright, Flutter-dev, SEO): [docs/MCP_CATALOG.md](docs/MCP_CATALOG.md).

---

## <img src="docs/icons/laptop.svg" width="22" alt="" /> Перенос состояния

Для миграции на новую систему достаточно перенести файл `~/.config/opencode/memory/memory.db` и директорию `notes/`, после чего запустить `./install.sh`.

---

## <img src="docs/icons/heart.svg" width="22" alt="" /> Поддержать проект

- **TON / USDT (TON)**: `UQAhzOYPIBQthqrwCFcIpsWUZjmi4KrK3BuGnjAQmJW04IC8`
- **USDT (TRC-20)**: `TGHVN7y5EhZ4pAreAXbffKY86kpq1dve9h`
- **Карты**: [Tribute](https://web.tribute.tg/d/Huc)

---

## <img src="docs/icons/license.svg" width="22" alt="" /> Лицензия

MIT License.
