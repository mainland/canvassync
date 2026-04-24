# CanvasSync

Synchronize local course content with Canvas.

## Usage

### Quick start

Install CanvasSync in the local virtual environment:

```bash
source .venv/bin/activate
pip install -e .
```

Create a starter config:

```bash
cp config.yaml.sample config.yaml
```

Edit `config.yaml` and set at least:

- `api_url`
- `course_sis`
- `modules`

If you do not know the `course_sis` value, see [Finding the course SIS ID](#finding-the-course-sis-id).

Then pass your Canvas API access token on the command line. If you do not have one yet, see [Canvas API access token](#canvas-api-access-token).

```bash
export CANVAS_API_KEY="your-canvas-api-token"
canvas-sync --config config.yaml --api-key "$CANVAS_API_KEY" courses
```

When the course lookup works, run a limited sync first:

```bash
canvas-sync --config config.yaml --api-key "$CANVAS_API_KEY" sync --limit "Week 1"
```

Then run the full sync:

```bash
canvas-sync --config config.yaml --api-key "$CANVAS_API_KEY" sync
```

### Commands

Global options must come before the subcommand:

```bash
canvas-sync --config config.yaml --api-key "$CANVAS_API_KEY" sync
```

Common commands:

- `courses`: list courses visible to the Canvas API token.
- `sync`: synchronize the configured syllabus, modules, pages, files, URLs, and assignment metadata.
- `sync --limit TEXT`: synchronize only modules whose names match `TEXT`. Repeat `--limit` to match more than one module.
- `render PATH`: render one Markdown file to HTML without updating Canvas pages or modules. Local images may still be uploaded during rendering.
- `render PATH --output rendered.html`: render Markdown to an output file.
- `roster`: print the course roster as a table.
- `roster --section TEXT`: print students from matching sections only.
- `roster --email`: print roster entries as email addresses.
- `dump`: print the current Canvas module and item outline for inspection.

### Before syncing

`sync` changes Canvas content. It can create, update, reorder, publish, unpublish, and delete module items to make Canvas match `config.yaml`.

Note the following:

- Module and item order in `config.yaml` matter and are reflected in Canvas.
- Extra Canvas module items after the configured items may be deleted.
- Assignments referenced with `assignment:` must already exist in Canvas.
- Local files referenced with `file:`, `page:`, `description:`, or `syllabus:` are resolved relative to the config file directory unless `--root` is provided.
- Run `sync --limit TEXT` first when testing changes to one module.

### Canvas API access token

CanvasSync needs a Canvas API access token for the `api_key` value.

To create one:

1. Sign in to Canvas in your browser.
2. In Global Navigation, open **Account**, then **Settings**.
3. Find the **Approved Integrations** section.
4. Click **Add New Access Token**.
5. Enter a purpose such as `CanvasSync`.
6. Choose an expiration date if appropriate.
7. Click **Generate Token**.
8. Copy the generated token before closing the dialog.

Treat this token like a password. It can access Canvas with your account's permissions. If the token is exposed, delete or regenerate it in Canvas. If the **Add New Access Token** button is disabled, your institution may require an administrator to manage access tokens.

See Instructure's guide: [How do I manage API access tokens in my user account?](https://community.instructure.com/en/kb/articles/662901-how-do-i-manage-api-access-tokens-in-my-user-account)

Prefer passing the token with `--api-key` instead of writing it into `config.yaml`:

```bash
canvas-sync --config config.yaml --api-key "$CANVAS_API_KEY" courses
```

Do not commit real Canvas tokens. Revoke any token that is accidentally exposed.

### Configuration file

CanvasSync reads course configuration from a YAML file passed with `--config`. Use `config.yaml.sample` as a starting point:

```bash
cp config.yaml.sample config.yaml
```

Minimal example:

```yaml
api_url: "https://drexel.instructure.com"
course_sis: "SIS EXAMPLE 202535"
syllabus: syllabus/syllabus.md

modules:
  - name: "Week 1"
    published: true
    items:
      - title: "Welcome"
        published: true
        page: pages/welcome.md

      - title: "Lecture Slides"
        published: true
        file: files/week-01-slides.pdf

      - title: "Assignment 1"
        published: true
        assignment: "Assignment 1"
        description: assignments/assignment-01.md
        points_possible: 100
        due_at: "1/19/2026 11:59pm"
```

Top-level keys:

- `api_url`: Canvas site URL, such as `https://canvas.instructure.com`.
- `api_key`: Canvas API access token. You can omit this when passing `--api-key` on the command line.
- `course_sis`: Canvas SIS course ID to synchronize. This value may contain spaces, so quote it in YAML.
- `pandoc_metadata`: optional metadata passed to Pandoc when rendering Markdown.
- `vars`: optional global variables promoted directly into Jinja templates. Use `vars` for short, frequently used values that you want to reference by name, such as `{{ instructor_name }}`.
- `data`: optional structured data available in Jinja templates as `site`. Use `data` for grouped course or site information, such as `{{ site.course.title }}`, to avoid cluttering the top-level template namespace.
- `syllabus`: optional Markdown file used to update the Canvas course syllabus.
- `modules`: ordered list of Canvas modules to create or synchronize.

Each module supports:

- `name`: module name.
- `published`: whether the module is published. Defaults to `false`.
- `unlock_at`: optional date/time string.
- `items`: ordered list of module items.

Each item needs a `title` and one content key. Nested `items` become indented module items.

- `page`: render a Markdown file and create or update a Canvas page.
- `page_contents`: render inline Markdown text and create or update a Canvas page.
- `file`: upload a local file and add it to the module.
- `url`: add an external URL. Use `new_tab: true` to open it in a new tab.
- `assignment`: link an existing Canvas assignment. Optional fields include `description`, `description_contents`, `points_possible`, `unlock_at`, `due_at`, and `lock_at`.
- `vars`: item-specific template variables. These are available when rendering that item's `page`, `page_contents`, assignment `description`, or assignment `description_contents`, and override top-level `vars` with the same name.
- No content key: create a Canvas module text header.

Paths are resolved relative to the config file directory unless `--root` is provided.

### Finding the course SIS ID

To find a course SIS ID in Canvas, open the course in your browser and go to **Settings**. On the **Course Details** tab, look for **SIS ID**. Copy that value exactly, including any spaces:

```yaml
course_sis: "SIS EXAMPLE 202535"
```

### Markdown rendering

CanvasSync renders Markdown before sending content to Canvas:

1. Jinja templates are expanded first.
2. Markdown is converted to HTML with Pandoc.
3. Bundled Lua filters are applied.
4. A co-located `.css` file is inlined when present.
5. Local images referenced by Markdown are uploaded to Canvas and rewritten to Canvas file preview URLs.

Values under `vars` are promoted directly into the template namespace. Values under `data` stay grouped under `site`.

Values under `pandoc_metadata` are passed to Pandoc as document metadata when Markdown is converted to HTML:

```yaml
pandoc_metadata:
  section: f2f
  term: "Spring 2026"
```

This is useful when Markdown, templates, or Pandoc filters need metadata that is not meant to be rendered directly as normal page content.

Bundled Lua filters are loaded automatically from `src/canvassync/filters`. Currently, `conditional.lua` supports section-specific content. It reads the Pandoc metadata value `section` and filters fenced Div blocks whose class starts with `only-`.

For example, with:

```yaml
pandoc_metadata:
  section: f2f
```

this Markdown keeps the `only-f2f` block and removes the `only-online` block:

```markdown
::: {.only-f2f}
This appears only for the face-to-face section.
:::

::: {.only-online}
This appears only for the online section.
:::
```

Any class matching `only-VALUE` is kept only when `pandoc_metadata.section` is `VALUE`.

Use top-level `vars` for short values that are convenient to reference directly throughout Markdown files:

```yaml
vars:
  instructor_name: "Ada Lovelace"
  office_hours: "Tuesdays 2-4pm"
```

Then use them by name in Markdown:

```markdown
Instructor: {{ instructor_name }}

Office hours: {{ office_hours }}
```

Use `data` for structured course data that should stay grouped under `site`:

```yaml
data:
  course:
    title: "Example Course"
    term: "Spring 2026"
  links:
    gradescope: "https://www.gradescope.com"
```

Then reference it through `site`:

```markdown
# {{ site.course.title }}

Term: {{ site.course.term }}

[Gradescope]({{ site.links.gradescope }})
```

Module items can also define `vars` for values that only apply to that item:

```yaml
modules:
  - name: "Week 1"
    items:
      - title: "Assignment 1"
        assignment: "Assignment 1"
        description: assignments/assignment-01.md
        vars:
          github_classroom_link: "https://classroom.github.com/example"
```

Those item-level variables are available while rendering `assignments/assignment-01.md` and take precedence over top-level variables with the same name.

## Development

This project uses [`pre-commit`](https://pre-commit.com/) to run local quality checks before each commit.

Install the development dependencies and Git hooks:

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

Run all configured hooks manually:

```bash
pre-commit run -a
```

The current pre-commit configuration runs:

- `ruff-check` for linting
- `docformatter` for docstring formatting checks
- `mypy` with the configuration in `pyproject.toml`

You can also run an individual hook:

```bash
pre-commit run mypy -a
pre-commit run docformatter -a
pre-commit run ruff-check -a
```
