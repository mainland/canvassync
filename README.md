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
- `course_sis` (or `course_id`)
- `modules`

If you do not know the `course_sis` value, see [Finding the course SIS ID](#finding-the-course-sis-id). Alternatively, set `course_id` to the numeric Canvas course ID, or pass `--course-id` on the command line.

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

Global options:

- `--config PATH`: course configuration file (required).
- `--root PATH`: resolve local paths relative to `PATH` instead of the config file directory.
- `--api-key KEY`: Canvas API token; overrides `api_key` from the config file.
- `--course-id ID`: numeric Canvas course ID; overrides both `course_id` and `course_sis` from the config file, so one config can target a sandbox course. See [Testing in a sandbox course](#testing-in-a-sandbox-course).
- `-n`, `--dry-run`: not implemented for `sync`; `sync` prints a warning and still changes Canvas.

Common commands:

- `courses`: list courses visible to the Canvas API token.
- `sync`: synchronize the configured assignment groups, syllabus, modules, pages, files, URLs, and assignments.
- `sync --limit TEXT`: synchronize only modules whose names match `TEXT`. Repeat `--limit` to match more than one module.
- `sync --create-assignments`: create assignments referenced with `assignment:` that do not exist in Canvas, instead of exiting with an error. Equivalent to `create_assignments: true` in the config.
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
- Assignments referenced with `assignment:` must already exist in Canvas unless `--create-assignments` (or `create_assignments: true`) is given.
- Assignment groups are created and updated, but never deleted.
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
- `course_sis`: Canvas SIS course ID to synchronize. This value may contain spaces, so quote it in YAML. Optional when `course_id` or `--course-id` is given.
- `course_id`: optional numeric Canvas course ID (the number in the course URL, `https://canvas.example.edu/courses/12345`). When present, it is used instead of `course_sis`. `--course-id` overrides both.
- `create_assignments`: optional; when `true`, `sync` creates missing assignments, as with `sync --create-assignments`. Defaults to `false`.
- `assignment_groups`: optional list of assignment groups to create or update. See [Assignment groups](#assignment-groups).
- `apply_assignment_group_weights`: optional; sets the course's "weight final grade based on assignment groups" option.
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
- `assignment`: link a Canvas assignment by name. Optional fields include `description`, `description_contents`, `unlock_at`, `due_at`, `lock_at`, and the [assignment settings](#assignment-settings) below.
- `vars`: item-specific template variables. These are available when rendering that item's `page`, `page_contents`, assignment `description`, or assignment `description_contents`, and override top-level `vars` with the same name.
- No content key: create a Canvas module text header.

Paths are resolved relative to the config file directory unless `--root` is provided.

### Assignment settings

Assignment items accept these settings. They are applied when an assignment is created and on every sync, but only when the Canvas value differs:

- `points_possible`: number of points.
- `submission_types`: list of submission types, such as `[online_upload]`, `[online_text_entry, online_url]`, `[on_paper]`, or `[none]`.
- `allowed_extensions`: list of file extensions allowed for `online_upload`, such as `[pdf, ipynb]`.
- `grading_type`: `points`, `percent`, `pass_fail`, `letter_grade`, `gpa_scale`, or `not_graded`.
- `assignment_group`: name of an assignment group. It must exist in Canvas or be listed under `assignment_groups`.
- `published`: the item's `published` flag also sets the assignment's published state when the assignment is created.

When `--create-assignments` is given and no assignment matches, CanvasSync creates one named by the `assignment` value (or the item `title` if `assignment` is not a string), with the settings above:

```yaml
- title: "HW1: Cellular Geometry"
  published: true
  assignment: "HW1: Cellular Geometry"
  description: assignments/homework.md
  points_possible: 20
  submission_types: [online_upload]
  allowed_extensions: [ipynb]
  grading_type: points
  assignment_group: Homework
  due_at: "1/10/2027 11:59pm"

- title: "Project Proposal"
  published: true
  assignment: "Project Proposal"
  submission_types: [online_upload]
  allowed_extensions: [pdf]
  assignment_group: Project
```

Assignments are looked up by the item `title` first, then by the `assignment` name.

### Assignment groups

The optional top-level `assignment_groups` list is synchronized at the start of every `sync`, before modules. Missing groups are created by name; existing groups are updated when `position`, `group_weight`, `drop_lowest`, or `drop_highest` differ. Groups are never deleted, and groups not listed are left alone.

```yaml
apply_assignment_group_weights: true

assignment_groups:
  - name: Homework
    position: 1
    group_weight: 40
    drop_lowest: 1
  - name: Quizzes
    position: 2
    group_weight: 20
  - name: Project
    position: 3
    group_weight: 40
```

Each group supports `name` (required), `position`, `group_weight` (percent, used when `apply_assignment_group_weights` is `true`), `drop_lowest`, and `drop_highest`.

### Testing in a sandbox course

Use `--course-id` to run an unchanged config against a sandbox or development course. It overrides both `course_id` and `course_sis`:

```bash
canvas-sync --course-id 12345 --config config.yaml --api-key "$CANVAS_API_KEY" \
  sync --create-assignments
```

`--create-assignments` creates the assignments that exist in the production course but not in the empty sandbox. Add `--limit "Week 1"` to try a single module first.

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

Run the test suite (the tests mock Canvas and make no network calls):

```bash
python -m pytest
```

You can also run an individual hook:

```bash
pre-commit run mypy -a
pre-commit run docformatter -a
pre-commit run ruff-check -a
```
