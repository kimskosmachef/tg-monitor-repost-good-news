# Конфигурация

Файлы с `.example` в имени — образцы. Лежат в git, не заполняются:
`config.example.yaml`, `sources.example.yaml`, `topics.example.yaml`.

Рабочие файлы — `config.yaml`, `sources.yaml`, `topics.yaml` — создаются
копированием образца без `.example` в имени и в git не попадают
(см. `.gitignore`):

```
cp config/config.example.yaml config/config.yaml
cp config/sources.example.yaml config/sources.yaml
cp config/topics.example.yaml config/topics.yaml
```

Формат каждого файла — §4 и §4.1 `docs/spec.md`.

Примеры граней и негативы (`examples_file`/`negatives_file` в `topics.yaml`)
живут в `config/examples/*.txt` — формат §4.2. Образцы — `config/examples/*.example.txt`,
реальные файлы создаются копированием (без `.example` в имени) и в git не попадают.
