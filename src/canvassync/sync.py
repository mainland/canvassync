import datetime
import logging
import sys
from functools import cached_property
from pathlib import Path
from typing import Any, List, Optional

import dateutil.parser
import jinja2
import pypandoc
import tzlocal
import yaml
from bs4 import BeautifulSoup
from canvasapi import Canvas
from canvasapi.assignment import Assignment
from canvasapi.course import Course
from canvasapi.file import File
from canvasapi.module import Module, ModuleItem
from rich import print

def normalize_html(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')

    return soup.prettify()

def html_equiv(a: str, b: str) -> bool:
    return normalize_html(a) == normalize_html(b)

def parse_datetime(date: str) -> datetime.datetime:
    """Parse a string date/time using local time zone.

    Args:
        date (str): A date to parse

    Returns:
        datetime.datetime: Parse datetime object
    """
    default_date = datetime.datetime.combine(datetime.datetime.now(),
                                             datetime.time(0, tzinfo=tzlocal.get_localzone()))

    return dateutil.parser.parse(date, default=default_date)

def item_type(item: dict) -> str:
    if "page" in item:
        return "Page"

    if "url" in item:
        return "ExternalUrl"

    if "file" in item:
        return "File"

    if "assignment" in item:
        return "Assignment"

    return "SubHeader"

class CanvasSync:
    config: dict
    """Canvas sync configuration"""

    root: Path
    """Root path"""

    def __init__(self, config_path: Path, root: Optional[Path]=None):
        with config_path.open("r", encoding="utf8") as f:
            self.config = yaml.safe_load(f)

        if root is None:
            self.root = config_path.parent
        else:
            self.root = root

    @property
    def api_url(self) -> str:
        """Canvas API URL"""
        return self.config['api_url']

    @property
    def api_key(self) -> str:
        """Canvas API key"""
        return self.config['api_key']

    @property
    def course_sis(self) -> str:
        """Course SIS"""
        return self.config['course_sis']

    @cached_property
    def canvas(self) -> Canvas:
        """Canvas API object"""
        return Canvas(self.api_url, self.api_key)

    @cached_property
    def course(self):
        """Canvas course"""
        kwargs = { "include[]": "syllabus_body" }

        return self.canvas.get_course(self.course_sis,
                                      use_sis_id=True,
                                      **kwargs)

    def render_markdown(self, path: Path) -> str:
        """Render Markdown (using pandoc)"""
        with open(self.root / path, 'r', encoding='utf8') as f:
            text = f.read()

        # Fill out template using launch parameters
        template = jinja2.Template(text)

        jinja_text = text = template.render(site=self.config['data'])

        # Render using pandoc
        with open(self.root / path, 'r', encoding='utf8') as f:
            return pypandoc.convert_text(jinja_text,
                                         to='html5+raw_html+smart',
                                         format='md',
                                         extra_args=['--mathjax'])

    def sync(self, limits: Optional[str]=None):
        """Synchronize course"""
        self.sync_syllabus(self.course)

        course_modules = self.course.get_modules()

        for idx, module in enumerate(self.config['modules']):
            if len(list(course_modules)) > idx:
                course_module = course_modules[idx]
            else:
                course_module = self.course.create_module(module={ 'name': module['name']
                                                                 , 'position': idx
                                                                 })

            self.sync_module(module, course_module, limits=limits)

    def sync_syllabus(self, course: Course):
        """Synchronize course syllabus"""
        if 'syllabus' in self.config:
            logging.debug("Rendering syllabus")

            html = self.render_markdown(self.config['syllabus'])

            if not html_equiv(course.syllabus_body, html):
                logging.debug("Updating course syllabus")
                course.update(course={'syllabus_body': html})

    def flatten_items(self, items: List[dict], indent: int=0) -> List[dict]:
        """Flatten nested module items"""
        result: List[dict] = []

        for item in items:
            item['indent'] = indent

            subitems = []
            if 'items' in item:
                subitems = self.flatten_items(item['items'], indent=indent+1)
                del item['items']

            result.append(item)
            result += subitems

        return result

    def sync_module(self, module: dict, course_module: Module, limits: Optional[str]=None):
        course_module.edit(module={ 'name': module['name']
                                  , 'published': module.get('published', False)
                                  })

        course_module_items = list(course_module.get_module_items())

        # Flatten module items
        module_items = self.flatten_items(module['items'])

        # Sync module items
        idx = 0
        course_idx = 0

        for item in module_items:
            if limits is not None and item['title'] not in limits:
                idx += 1
                course_idx += 1
                continue

            if len(course_module_items) > idx:
                module_item = course_module_items[course_idx]
                if not self.sync_module_item(course_module, idx, item, module_item):
                    course_idx += 1
            else:
                module_item = self.create_module_item(course_module, idx, item)

                if self.sync_module_item(course_module, idx, item, module_item):
                    raise ValueError("Should not have created a module item!")

            idx += 1

        for course_module_item in course_module_items[course_idx:]:
            logging.debug("Deleting extra item: %s", course_module_item)
            course_module_item.delete()

    def get_assignment(self, name: str) -> Optional[Assignment]:
        assignments = self.course.get_assignments()

        for assignment in assignments:
            if assignment.name == name:
                return assignment

        return None

    def get_file(self, filename: str) -> Optional[File]:
        for file in self.course.get_files():
            if file.filename == filename:
                return file

        return None

    def upload_file(self, filepath: Path) -> File:
        # Bail if the file doesn't exist
        if not filepath.exists():
            raise ValueError(f"File '{filepath:}' does not exist")

        # See if a file by this name exists
        file: Optional[File] = self.get_file(filepath.name)

        # If so, compare contents
        if file is not None:
            old_contents = file.get_contents(binary=True)

            with filepath.open('rb') as f:
                new_contents = f.read()

            # Contents differ. Delete the old file and force an upload.
            if new_contents != old_contents:
                logging.debug("File contents differ")
                file.delete()
                file = None
            else:
                logging.debug("File contents identical")

        if file is not None:
            return file

        if file is None:
            logging.debug("Uploading '%s'", str(filepath))
            success, response = self.course.upload(filepath)
            if success:
                return self.canvas.get_file(response['id'])

            raise ValueError(f"Could not upload {filepath:}")

    def create_module_item(self, course_module: Module, idx: int, item: dict) -> ModuleItem:
        the_type = item_type(item)

        if the_type == "Page":
            html = self.render_markdown(item['page'])
            page = self.course.create_page(wiki_page={ 'title': item['title']
                                                     , 'body': html
                                                     })

            return course_module.create_module_item(module_item={ 'title': item['title']
                                                                , 'type': "Page"
                                                                , 'page_url': page.url
                                                                , 'position': idx+1
                                                                })
        elif the_type == "ExternalUrl":
            return course_module.create_module_item(module_item={ 'title': item['title']
                                                                , 'type': "ExternalUrl"
                                                                , 'external_url': item['url']
                                                                , 'position': idx+1
                                                                })
        elif the_type == "SubHeader":
            return course_module.create_module_item(module_item={ 'title': item['title']
                                                                , 'type': "SubHeader"
                                                                , 'position': idx+1
                                                                })
        elif the_type == "File":
            filepath: Path = self.root / Path(item['file'])
            file: File = self.upload_file(filepath)

            return course_module.create_module_item(module_item={ 'title': item['title']
                                                                , 'type': "File"
                                                                , 'position': idx+1
                                                                , 'content_id': file.id
                                                                })
        elif the_type == "Assignment":
            assignment = self.get_assignment(item['title'])

            if assignment is None:
                print(f"[red]Cannot find assignment {item['title']:}[/red]", file=sys.stderr)
                sys.exit(1)

            return course_module.create_module_item(module_item={ 'title': item['title']
                                                                , 'type': "Assignment"
                                                                , 'position': idx+1
                                                                , 'content_id': assignment.id
                                                                })
        else:
            raise ValueError(f"Can't create item type {the_type:}")

    def sync_module_item(self, course_module: Any, idx: int, item: dict, course_item: Any) -> bool:
        logging.debug("Synchronizing %s (%s)", course_item.title, course_item.type)

        the_type = item_type(item)

        created: bool = False

        if the_type != course_item.type:
            course_item = self.create_module_item(course_module, idx, item)
            created = True

        if the_type == "Page":
            html = self.render_markdown(item['page'])
            page = self.course.get_page(course_item.page_url)

            if page.body is None or not html_equiv(html, page.body):
                page.edit(wiki_page={'body': html})
        elif the_type == "ExternalUrl":
            if course_item.external_url != item['url']:
                course_item.edit(module_item={ 'external_url': item['url'], 'new_tab': False })
        elif the_type == "SubHeader":
            pass
        elif the_type == "File":
            filepath: Path = self.root / Path(item['file'])

            if not filepath.exists():
                print(f"[red]Not updating because file '{filepath:}' does not exist[/red]")
            else:
                file: File = self.upload_file(filepath)

                # XXX We can't update a file link even though the REST API
                # doesn't throw an error, so delete the old module item and
                # create a new one
                if False:
                    course_item = course_item.edit(module_item={'content_id': file.id, 'type': 'File'})
                else:
                    if course_item.content_id != file.id:
                        course_item.delete()
                        course_item = course_module.create_module_item(module_item={ 'title': item['title']
                                                                                   , 'type': "File"
                                                                                   , 'position': idx+1
                                                                                   , 'content_id': file.id
                                                                                   })
                        created = True
        elif the_type == "Assignment":
            assignment = self.get_assignment(item['title'])

            if assignment is None:
                print("[red]Cannot find assignment %s[/red]", file=sys.stderr)
                sys.exit(1)

            course_item.edit(module_item={ 'content_id': assignment.id })

            if 'description' in item:
                html = self.render_markdown(item['description'])

                if assignment.description is None or not html_equiv(assignment.description, html):
                    assignment.edit(assignment={'description': html })

            for date_attr in ['due_at', 'lock_at', 'unlock_at']:
                if date_attr in item:
                    dt = parse_datetime(item[date_attr])
                    assignment.edit(assignment={date_attr: dt.isoformat()})
        else:
            raise ValueError(f"Can't handle item type {the_type:}")

        logging.debug("Updating item %s (indent %d)", item['title'], item['indent'])
        if course_item.type == "File":
            course_item.edit(module_item={ 'title': item['title']
                                         , 'indent': item['indent']
                                         })

            file = self.course.get_file(course_item.content_id)
            file.update(hidden=not item.get('published', False))
        else:
            course_item.edit(module_item={ 'title': item['title']
                                         , 'indent': item['indent']
                                         , 'published': item.get('published', False)
                                         })

        return created
