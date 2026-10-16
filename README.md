# iren-mcp

Независимый MCP-сервер и CLI для создания, проверки и визуального предпросмотра тестов Айрен в формате `.itx`.

Проект работает без сторонних Python-зависимостей и поддерживает Python 3.8+. MCP-сервер использует стандартный `stdio`-транспорт: клиент запускает Python-процесс и обменивается с ним JSON-RPC сообщениями по `stdin`/`stdout`.

На Windows сначала используйте `py`; если launcher не настроен, укажите полный путь к `python.exe` или запускайте команды через `uv run --no-project python`.

## Возможности

- создание и сборка `.itx` из JSON-спецификации;
- перепаковка существующего теста с сохранением `test.xml` и изображений;
- `test_info` — дерево секций, профили, теги, картинки, формулы, сценарии и ответы для авторской сверки;
- `unpack_test` — безопасная распаковка только разрешённых членов `.itx` (`test.xml`, `images/*.png|*.jpg` и связанные `images/*.dat`);
- структурная проверка ZIP/XML, вопросов, ответов, тегов, профилей и изображений;
- статическая проверка inline-формул, эталонов, маркеров `$(variable)` и Pascal-подобных сценариев;
- автономный HTML-предпросмотр всех вопросов, изображений и заявленных правильных ответов;
- `open_in_iren` — после успешной проверки открыть `.itx` через ассоциацию установленной Айрен в Windows;
- безопасная обработка архивов: запрет traversal-путей, symlink, DTD/entities, неизвестных ZIP-файлов и чрезмерных размеров;
- CLI для локальной автоматизации.

Проверка сценариев и формул в основном статическая: она проверяет ограничения формата Айрен, баланс конструкций, объявления переменных, маркеры и числовые шаблоны. Она не выполняет случайный Pascal-код и не доказывает предметную правильность ответа. Для формул доступна дополнительная ручная проверка через HTML/редактор Айрен; внешний MathJax-рендерер не встраивается в стандартную Python-only установку.

## Запуск MCP

```powershell
py -m pip install .
py "C:\AI Projects\MCP для тестов\iren_mcp_server.py" --stdio
```

Пример конфигурации stdio-клиента:

```json
{
  "mcpServers": {
    "iren-mcp": {
      "command": "py",
      "args": ["C:/path/to/iren_mcp_server.py", "--stdio"]
    }
  }
}
```

Сервер предоставляет инструменты `create_test`, `build_test`, `test_info`, `unpack_test`, `validate_formulas`, `validate_test`, `render_html` и `open_in_iren`.

## CLI

```powershell
py iren_mcp_server.py validate --itx path/to/test.itx
py iren_mcp_server.py validate --itx path/to/test.itx --check-near-dups
py iren_mcp_server.py validate-formulas --itx path/to/test.itx
py iren_mcp_server.py info --itx path/to/test.itx --answers
py iren_mcp_server.py unpack --itx path/to/test.itx --output path/to/unpacked
py iren_mcp_server.py render --itx path/to/test.itx --output path/to/preview --overwrite
py iren_mcp_server.py create --spec path/to/spec.json --output path/to/new.itx
py iren_mcp_server.py build --itx path/to/test.itx --output path/to/repacked.itx
```

По умолчанию существующие выходные файлы не перезаписываются. Для этого нужно явно передать `--overwrite` или `overwrite: true` через MCP.

## JSON-спецификация

Минимальный пример:

```json
{
  "sectionTitle": "Тема",
  "questions": [
    {
      "type": "select",
      "content": "Выберите правильный вариант.",
      "choices": [
        {"text": "Правильный ответ", "correct": true},
        {"text": "Дистрактор"}
      ]
    },
    {
      "type": "input",
      "content": "Введите ответ.",
      "patterns": [{"value": "Ответ"}]
    }
  ]
}
```

Расширенная схема для `match`, `order`, `classify`, формул, изображений, профилей и сценариев находится в [skills/iren-test-creation/references/schema.md](skills/iren-test-creation/references/schema.md). Правила подготовки содержательно качественных заданий находятся в [skills/iren-test-creation/SKILL.md](skills/iren-test-creation/SKILL.md).

## Добавление в Codex как marketplace

В репозитории уже есть Codex marketplace-манифест `.agents/plugins/marketplace.json`, portable-манифест `plugin.json`, compatibility-манифест `.codex-plugin/plugin.json` и MCP-конфигурации `mcp.json`/`.mcp.json`.

После установки Codex CLI добавьте marketplace:

```powershell
codex plugin marketplace add Super-Den/iren-mcp --ref main
codex plugin marketplace list
```

Затем перезапустите Codex/ChatGPT desktop, откройте Plugins Directory, выберите marketplace `Айрен MCP` и установите `Айрен ITX MCP`. Плагин является desktop/local-executor пакетом: в Windows сервер запускается локальным launcher `py`, поэтому на компьютере должен быть Python 3.8+. В Linux/macOS при необходимости замените `py` на `python3` в локальной MCP-конфигурации.

Если marketplace уже добавлен, обновите его:

```powershell
codex plugin marketplace upgrade iren-mcp-marketplace
```

Marketplace и plugin-манифесты нужны для установки в Codex. Сам `.gitignore` не является манифестом и не заменяет эти файлы.

`open_in_iren` запускает только уже выбранный локальный `.itx` через системную ассоциацию и принимает файл после `validate_test`. Публичного стабильного API редактора Айрен нет: его локальный WebSocket/Protobuf companion-протокол требует ephemeral editor key и зависит от версии, поэтому MCP не подключается к нему напрямую и не принимает произвольные команды ОС.

## Юридические границы и атрибуция

Это независимый инструмент для работы с форматом тестов Айрен. «Айрен» и связанные материалы принадлежат их правообладателям; проект не заявляет аффилированность с автором Айрен Сергеем Останиным.

В репозитории не распространяются предоставленные пользователем книги, исходные `.itx`-тесты, OCR-слои, изображения и копия официального HTML-теста. Их права и условия распространения нужно проверять отдельно. Официальная страница Айрен указывает, что новые версии программы с открытым исходным кодом распространяются под Apache 2.0, а для старой версии 2.3 действует отдельная лицензия с ограничениями на распространение дистрибутива: [лицензия Айрен](https://irenproject.ru/license). Это не даёт автоматически права публиковать сторонние тесты или книгу.

Собственный код этого репозитория распространяется под [MIT License](LICENSE). Лицензия требует сохранять уведомление об авторских правах и не даёт гарантий.

## Использование ИИ

Проект разработан с помощью ИИ при участии и проверке владельца репозитория. Для программного кода отдельная обязательная пометка об использовании ИИ обычно не заменяет лицензию и не передаёт права на сторонние материалы. Такая пометка оставлена в README для прозрачности; при публикации тестов, фрагментов книги или изображений нужно указывать их собственные источники и лицензии.

## Проверки перед публикацией

```powershell
py -m unittest discover -s . -p "test_*.py" -v
py -m py_compile iren_mcp_server.py skills/iren-test-creation/scripts/iren_mcp_server.py
```

MCP соблюдает stdio-правила: stdout содержит только JSON-RPC сообщения, а диагностические ошибки возвращаются как MCP tool results с `isError: true`.
