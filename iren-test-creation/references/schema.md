# Схема `.itx` и JSON-спецификация MCP

## Контейнер

`.itx` — ZIP-архив. В корне находится `test.xml`; изображения обычно лежат в `images/`. XML использует UTF-8 и корень:

```xml
<irenTest version="6">
  <section title="Раздел">
    <questions>...</questions>
    <sections>...</sections>
  </section>
  <profiles>...</profiles>
  <testInfo lastGeneratedTag="N"/>
</irenTest>
```

`tag` — уникальный положительный идентификатор вопроса, он не обязан быть непрерывным. `weight` должен быть положительным, `enabled` — `true` или `false`.

## Содержимое

Внутри `content` редактор Айрен использует последовательность узлов:

```xml
<content>
  <text value="Текст"/>
  <br/>
  <img src="images/1.png"/>
  <formula source="m"/>
</content>
```

Путь изображения относительный и не должен выходить из архива. В HTML-предпросмотре изображения копируются в каталог `images/`.

## Типы вопросов

```xml
<question type="select" tag="1" weight="1" enabled="true">
  <content><text value="..."/></content>
  <choices>
    <choice correct="true" fixed="false" negative="false">
      <content><text value="..."/></content>
    </choice>
  </choices>
</question>
```

У `select` должен быть хотя бы один `correct="true"`. Если правильный ответ добавляется редактором автоматически, допустим модификатор `addNegativeChoice`; это всё равно нужно проверить в предпросмотре.

```xml
<question type="input" ...>
  <content>...</content>
  <patterns>
    <pattern type="text" value="Ответ" quality="1.0000"
             caseSensitive="false" spaces="normalize"
             numericAware="false" wildcard="false"/>
  </patterns>
</question>
```

Для чисел используют `numericAware="true"` и осмысленный `precision`. Несколько `pattern` задают эквивалентные ответы.

```xml
<question type="match" ...>
  <content>...</content>
  <matchOptions baseItemsUsed="all" distractorsUsed="all"/>
  <pairs>
    <pair>
      <baseItem><content>...</content></baseItem>
      <matchingItem><content>...</content></matchingItem>
    </pair>
  </pairs>
</question>
```

`order` хранит эталон в `sequence/sequenceItem`; `distractors/distractor` — необязательные отвлекающие элементы. `classify` хранит категории в `categories/category`, заголовок в `categoryTitle`, элементы в `categoryItems/categoryItem`.

## Сценарий

```xml
<modifiers>
  <modifier type="script">
    <script line="var"/>
    <script line="  a, b: Integer;"/>
    <script line="begin"/>
    <script line="  a := Random(10);"/>
    <script line="  b := a + 1;"/>
    <script line="end."/>
  </modifier>
</modifiers>
```

Вопрос и варианты могут содержать `$(a)`, `$(b)`. Валидация проверяет синтаксические признаки и связь маркеров с переменными, но не заменяет выполнение сценария в Айрен.

## Профили

Профиль обычно содержит `questionSelection`, `sessionOptions`, `testResults`, `markScale` и модификаторы профиля:

```xml
<profiles>
  <profile title="Обучение">
    <questionSelection questionsPerSection="all" shuffleQuestions="true"/>
    <sessionOptions durationMinutes="unlimited" editableAnswers="true"
                    browsableQuestions="true" weightCues="false"/>
    <testResults percentCorrect="true" points="true" mark="true"/>
    <markScale><marks>
      <mark title="2" lowerBound="0.0000"/>
      <mark title="3" lowerBound="0.6500"/>
    </marks></markScale>
    <modifiers><modifier type="shuffleChoices"/></modifiers>
  </profile>
</profiles>
```

Границы `lowerBound` лежат от 0 до 1 и идут по возрастанию. Профили с `sectionProfile` позволяют задавать выборку по вложенным разделам.

## JSON DSL `create_test`

Строка становится `<text>`, список может смешивать строки и объекты `{"text": ...}`, `{"img": "images/1.png"}`, `{"br": true}`, `{"formula": "m"}`. Минимальная спецификация:

```json
{
  "sectionTitle": "Тема",
  "questions": [
    {"type": "select", "content": "...",
     "choices": [
       {"text": "Правильно", "correct": true},
       {"text": "Дистрактор"}
     ]},
    {"type": "input", "content": "...",
     "patterns": [{"value": "Ответ"}]},
    {"type": "match", "content": "...",
     "pairs": [{"base": "A", "match": "1"}, {"base": "B", "match": "2"}]},
    {"type": "order", "content": "...", "sequence": ["1", "2"]},
    {"type": "classify", "content": "...",
     "categories": [{"title": "X", "items": ["a"]}, {"title": "Y", "items": ["b"]}]}
  ],
  "images": {"images/1.png": "C:/path/to/1.png"}
}
```

`output` задаётся отдельно в вызове MCP. Полный пример профиля и сценария находится в комментариях к `iren_mcp_server.py` и может быть передан как JSON-файл.
