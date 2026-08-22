# <img src="icons/tools.svg" width="26" alt="" /> Каталог MCP-серверов

Crystallized даёт агенту память. Этот каталог даёт агенту руки: навигацию по коду, браузер, мобильное устройство, аналитику и видеомонтаж.

Каждый сервер — отдельный модуль. Ставится независимо, включается одной вставкой в `opencode.json`, отключается флагом `"enabled": false`. Ничего из этого не требуется для работы самой памяти.

Готовые шаблоны лежат в [`config/mcp/`](../config/mcp/).

---

## <img src="icons/alert.svg" width="22" alt="" /> Правило безопасности

В этом репозитории **нет и не должно быть настоящих ключей**. Все шаблоны содержат только заглушки. Перед коммитом любой правки проверь, что ты не вписал реальный токен в JSON.

Секреты держи вне конфига — в отдельных файлах:

```bash
mkdir -p ~/.config/opencode/secrets
chmod 700 ~/.config/opencode/secrets
printf '%s' 'YOUR_REAL_TOKEN' > ~/.config/opencode/secrets/yandex-oauth-token
chmod 600 ~/.config/opencode/secrets/yandex-oauth-token
```

В конфиге ссылайся на файл, а не на значение:

```json
"YANDEX_WEBMASTER_OAUTH_TOKEN": "{file:~/.config/opencode/secrets/yandex-oauth-token}"
```

Синтаксис `{file:...}` opencode разворачивает при старте: токен читается с диска и никогда не попадает в git.

---

## <img src="icons/workflow.svg" width="22" alt="" /> Как подключить любой сервер

1. Открой нужный шаблон в `config/mcp/`.
2. Замени заглушки на свои пути и значения (таблица заглушек — ниже).
3. Скопируй содержимое ключа `mcp` в свой `~/.config/opencode/opencode.json`, **не затирая** уже существующие серверы — в том числе `memory` от Crystallized.
4. Перезапусти opencode.

Пример результата слияния двух серверов:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memory": {
      "type": "local",
      "enabled": true,
      "command": ["uv", "run", "--project", "MEMORY_PATH", "python", "MEMORY_PATH/server.py"]
    },
    "serena": {
      "type": "local",
      "enabled": true,
      "command": ["SERENA_PATH", "start-mcp-server", "--context=ide", "--project-from-cwd", "--open-web-dashboard", "false"]
    }
  }
}
```

Слить файлы можно и командой (требуется `jq`):

```bash
jq -s '.[0] * .[1]' ~/.config/opencode/opencode.json config/mcp/serena.json > /tmp/merged.json
mv /tmp/merged.json ~/.config/opencode/opencode.json
```

Проверить, что конфиг остался валидным:

```bash
python3 -c "import json; json.load(open('$HOME/.config/opencode/opencode.json')); print('OK')"
```

### Заглушки в шаблонах

| Заглушка | Чем заменить | Как узнать |
| --- | --- | --- |
| `YOUR_HOME` | Домашний каталог | `echo $HOME` |
| `UV_PATH` | Путь к `uv` | `which uv` |
| `SERENA_PATH` | Путь к `serena` | `which serena` |
| `CODEBASE_MEMORY_PATH` | Путь к `codebase-memory-mcp` | `which codebase-memory-mcp` |
| `FCP_MCP_PATH` | Путь к `fcp-mcp` | `which fcp-mcp` |
| `DESIGN_COCKPIT_PATH` | Каталог с `server.py` design-cockpit | путь клона репозитория |
| `FLUTTER_MCP_SERVER_PATH` | Каталог с `server.py` flutter-dev | путь клона репозитория |
| `YOUR_PROJECTS_ROOT` | Корень, внутри которого лежат репозитории | например `~/Documents/projects` |
| `YOUR_FLUTTER_PROJECT_PATH` | Корень Flutter-проекта | путь к `pubspec.yaml` |
| `YOUR_API_KEY`, `YOUR_REAL_TOKEN` | Твой ключ | панель соответствующего сервиса |

Абсолютные пути надёжнее: opencode запускает серверы не из твоей интерактивной оболочки, и `PATH` там может отличаться. Именно поэтому в части шаблонов `PATH` задан явно.

---

## <img src="icons/brain.svg" width="22" alt="" /> serena — интеллект по коду

**Файл:** `config/mcp/serena.json`

Навигация по проекту на уровне символов, а не строк. Агент находит определение класса, все ссылки на метод, переименовывает символ по всему репозиторию и правит тело функции, не переписывая файл целиком. Работает через LSP, понимает десятки языков.

Зачем: вместо чтения десяти файлов целиком агент забирает ровно нужный символ. Меньше контекста, меньше ошибок, меньше денег.

**Требования:** Python 3.11+ и `uv`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --from git+https://github.com/oraios/serena serena
which serena
```

Подставь вывод `which serena` в `SERENA_PATH`. Флаг `--project-from-cwd` заставляет сервер брать проект из текущего рабочего каталога, `--open-web-dashboard false` отключает открытие браузера при старте.

---

## <img src="icons/folder.svg" width="22" alt="" /> codebase-memory — граф знаний о репозитории

**Файл:** `config/mcp/codebase-memory.json`

Строит граф всего кода: функции, классы, маршруты, вызовы, импорты, межсервисные HTTP-переходы. Дальше по этому графу можно спрашивать: кто вызывает эту функцию, что сломается при её изменении, какие есть архитектурные кластеры, где узкие места по цикломатической сложности и вложенным циклам.

Зачем: анализ влияния правки до того, как правка сделана.

**Требования:** исполняемый файл `codebase-memory-mcp` в `PATH`.

Переменные окружения:

- `CBM_ALLOWED_ROOT` — единственный каталог, который серверу разрешено индексировать. Ставь его как можно уже: это граница доступа.
- `CBM_CACHE_DIR` — где хранить индексы.
- `CBM_LOG_LEVEL` — `warn` в обычной работе, `debug` при разборе проблем.

Первую индексацию проекта запускает сам агент, она занимает от секунд до нескольких минут в зависимости от размера репозитория.

---

## <img src="icons/laptop.svg" width="22" alt="" /> playwright — браузер под управлением агента

**Файл:** `config/mcp/playwright.json`

Реальный Chrome: переходы по страницам, клики, заполнение форм, скриншоты, чтение консоли и сетевых запросов, снимок дерева доступности. Агент проверяет собственную вёрстку глазами, а не догадками.

Зачем: E2E-проверки, воспроизведение баг-репортов, снятие доказательств того, что фикс действительно работает.

**Требования:** Node.js 18+ и `npx` (входит в состав npm). Пакет `@playwright/mcp@latest` подтягивается автоматически при первом запуске.

```bash
node --version
npx -y @playwright/mcp@latest --help
```

Если Chrome не установлен, поставь браузеры Playwright:

```bash
npx -y playwright install chrome
```

Флаг `--browser chrome` можно заменить на `chromium`, `firefox` или `webkit`.

---

## <img src="icons/flash.svg" width="22" alt="" /> design-cockpit — дизайн-брифы и аудит доступности

**Файл:** `config/mcp/design-cockpit.json`

Дисциплина для интерфейсной работы. Сервер заводит сессию дизайна, находит в проекте существующие компоненты и токены, снимает скриншоты в трёх ширинах экрана, прогоняет проверки доступности и линтует код на захардкоженные цвета и инлайновые стили.

Зачем: чтобы агент не выдумывал новую кнопку, когда в проекте уже есть готовая, и чтобы результат подтверждался скриншотом, а не словами.

**Требования:** Python 3.11+, `uv`, установленные браузеры Playwright для съёмки скриншотов.

`DESIGN_COCKPIT_SESSION_DIR` указывает, где хранить сессии и снимки. Каталог создаётся автоматически.

---

## <img src="icons/tools.svg" width="22" alt="" /> flutter-dev — управление Android-устройством

**Файл:** `config/mcp/flutter-dev.json`

Полный цикл мобильной разработки: `dart analyze` без пересборки, сборка и установка APK, запуск приложения в сессии tmux, горячая перезагрузка, чтение logcat, скриншоты с устройства и эмуляция тапов и свайпов.

Зачем: агент видит экран телефона и может сам пройти сценарий, вместо того чтобы просить об этом тебя.

**Требования:** Flutter SDK, Android SDK с `adb` в `PATH`, `tmux`, Python 3.11+ и `uv`. Устройство подключено и авторизовано.

```bash
flutter doctor
adb devices
```

Переменные окружения:

- `FLUTTER_MCP_PROJECT` — корень Flutter-проекта по умолчанию.
- `FLUTTER_MCP_PACKAGE` — имя Android-пакета, например `com.example.yourapp`.
- `FLUTTER_MCP_APK_RELATIVE` — путь к собранному APK относительно корня проекта.

---

## <img src="icons/key.svg" width="22" alt="" /> seo-analytics — Search Console, Вебмастер и Метрика

**Файл:** `config/mcp/seo-analytics.json`

Три сервера в одном шаблоне. Включай только те, что нужны, остальные удали из фрагмента.

**Google Search Console** (`mcp-server-gsc`) — запросы, показы, клики, средняя позиция, инспекция индексации URL, работа с картами сайта.

Требуется сервисный аккаунт Google Cloud с включённым Search Console API. JSON-ключ положи вне репозитория и пропиши путь к нему в `GOOGLE_APPLICATION_CREDENTIALS`. Затем выдай адресу сервисного аккаунта права на ресурс в интерфейсе Search Console.

**Яндекс Вебмастер** (`yandex-webmaster-mcp-server`) — ИКС, индексация, битые ссылки, поисковые запросы, переобход страниц.

**Яндекс Метрика** (`@theyahia/yandex-metrika-mcp`) — визиты, источники трафика, популярные страницы, цели и конверсии.

Оба сервиса Яндекса используют один OAuth-токен: получи его в Яндекс OAuth для приложения с доступом к Вебмастеру и Метрике, сохрани в `~/.config/opencode/secrets/yandex-oauth-token` и подключай через `{file:...}`. В `YANDEX_WEBMASTER_HOST_URL` укажи свой домен целиком, вместе со схемой.

**Требования:** Node.js 18+ и `npx`.

---

## <img src="icons/laptop.svg" width="22" alt="" /> fcp — автоматизация Final Cut Pro

**Файл:** `config/mcp/fcp.json`

Работа с монтажными проектами через FCPXML: сборка таймлайна из списка клипов, маркеры, титры, переходы, роли, анализ ритма нарезки, отчёт контроля качества, экспорт в EDL и в формат DaVinci Resolve. Плюс анализ медиафайлов через ffprobe: длительность, кодеки, тишина, смены сцен, громкость по EBU R128.

Зачем: рутина монтажа, которую руками делать долго, а описать словами — быстро.

**Требования:** macOS, Final Cut Pro для живого управления приложением, `ffmpeg` и `ffprobe` для анализа медиа. Для автоматизации меню приложения потребуется выдать терминалу права в разделе «Универсальный доступ» системных настроек.

```bash
brew install ffmpeg
```

`FCP_PROJECTS_DIR` — каталог по умолчанию для чтения и записи файлов `.fcpxml`.

---

## <img src="icons/license.svg" width="22" alt="" /> Порядок отладки

Если сервер не поднялся:

1. Проверь, что бинарник существует и запускается вручную: `which serena`, `npx -y @playwright/mcp@latest --help`.
2. Проверь синтаксис конфига: `python3 -c "import json; json.load(open('$HOME/.config/opencode/opencode.json'))"`.
3. Убедись, что в `command` абсолютные пути, а не имена команд, которые есть только в твоей оболочке.
4. Временно поставь `"enabled": false` проблемному серверу — остальные продолжат работать.
5. Для серверов на `npx` первый запуск скачивает пакет и может занять до минуты.
