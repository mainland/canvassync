import datetime
import logging
import re
import sys
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple, TypeAlias

import dateutil.parser
import pypandoc
import tzlocal
import yaml
from bs4 import BeautifulSoup
from canvasapi import Canvas
from canvasapi.assignment import Assignment
from canvasapi.course import Course
from canvasapi.file import File
from canvasapi.folder import Folder
from canvasapi.module import Module, ModuleItem
from canvasapi.page import Page
from jinja2 import Environment, FileSystemLoader
from rich import print

TextSource: TypeAlias = Path | str
"""A text source. Either a path to a file or a raw string."""

def normalize_html(html: str) -> str:
    soup = BeautifulSoup(html, 'lxml')

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

def make_filter(keywords: Optional[List[str]], regexps: Optional[List[str]]=None) -> Optional[Callable[[str], bool]]:
    """Create a string filter predicate.

    The returned predicate returns true for any string that matches any of the
    specified keywords or regular expressions.

    Args:
        keywords (Optional[List[str]]): Keywords to match.
        regexps (Optional[List[str]], optional): Regular expressions to match. Defaults to None.

    Returns:
        Optional[Callable[[str], bool]]: Returns a filter or None if no
          keywords or regular expressions were specified.
    """
    if keywords is None:
        keywords = []

    if regexps is None:
        regexps = []

    if len(keywords) == 0 and len(regexps) == 0:
        return None

    regexp = "|".join([re.escape(k) for k in keywords] + regexps)
    pat = re.compile(regexp)

    return lambda s : bool(pat.search(s))

def flatten_items(items: List[dict], indent: int=0) -> List[dict]:
    """Flatten nested module items"""
    result: List[dict] = []

    for item in items:
        item['indent'] = indent

        subitems = []
        if 'items' in item:
            subitems = flatten_items(item['items'], indent=indent+1)
            del item['items']

        result.append(item)
        result += subitems

    return result

def item_type(item: dict) -> str:
    if "page" in item:
        return "Page"

    if "page_contents" in item:
        return "Page"

    if "url" in item:
        return "ExternalUrl"

    if "file" in item:
        return "File"

    if "assignment" in item:
        return "Assignment"

    return "SubHeader"

def get_page_source(item: dict) -> TextSource:
    assert item_type(item) == "Page"

    if 'page' in item:
        return Path(item['page'])
    else:
        return item['page_contents']

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

    def get_assignment(self, name: str) -> Optional[Assignment]:
        assignments = self.course.get_assignments()

        for assignment in assignments:
            if assignment.name == name:
                return assignment

        return None

    def get_folder(self, folderpath: str) -> Optional[Folder]:
        for folder in self.course.get_folders():
            if folder.name == folderpath:
                return folder

        return None

    def get_file(self, filename: str, parent_folder_path: Optional[str] = None) -> Optional[File]:
        folder: Optional[Folder] = None

        if parent_folder_path is not None:
            folder = self.get_folder(parent_folder_path)
            if folder is None:
                return None

        for file in self.course.get_files():
            if file.filename == filename:
                # If folder was specified, make sure file is in folder
                if folder is not None:
                    if file.folder_id == folder.id:
                        return file
                else:
                    return file

        return None

    def upload_file(self, filepath: Path, check_contents: bool = False) -> Tuple[bool, File]:
        """Upload a file.

        If the file already exists on Canvas and does not need to be updated,
        return the existing File object.

        Args:
            filepath (Path): Path to file. check_contents (bool, optional):
            Always check file contents. Defaults to False.

        Returns:
            Tuple[bool, File]: flag indicating if file was uploaded and File
            object.
        """
        # Bail if the file doesn't exist
        if not filepath.exists():
            raise FileNotFoundError(f"File '{filepath:}' does not exist")

        # Determine file's parent folder
        parent_folder_path: str = str(filepath.relative_to(self.root).parent)

        # See if a file by this name exists
        file: Optional[File] = self.get_file(filepath.name, parent_folder_path=parent_folder_path)

        # If so, check if file has been modified
        if file is not None:
            if check_contents:
                logging.debug("Getting file contents")
                old_contents = file.get_contents(binary=True)

                with filepath.open('rb') as f:
                    new_contents = f.read()

                # Contents differ. Delete the old file and force an upload.
                if new_contents != old_contents:
                    logging.debug("File contents differ")
                    file = None
                else:
                    logging.debug("File contents identical")
            else:
                modified = datetime.datetime.fromtimestamp(filepath.stat().st_mtime, tz=datetime.timezone.utc)

                if modified > file.modified_at_date:
                    logging.debug("File modified")
                    file = None

        if file is not None:
            return False, file

        if file is None:
            logging.debug("Uploading '%s'", str(filepath))
            success, response = self.course.upload(filepath, parent_folder_path=parent_folder_path)
            if success:
                return True, self.canvas.get_file(response['id'])

            raise ValueError(f"Could not upload {filepath:}")

    def get_page(self, id_or_url: str) -> Page:
        return self.course.get_page(id_or_url)

    def get_page_by_title(self, title: str) -> Optional[Page]:
        pages = list(self.course.get_pages(search_term=title))
        if len(pages) == 0:
            return None
        elif len(pages) == 1:
            return self.get_page(pages[0].page_id)
        else:
            raise ValueError(f"Multiple pages match '{title:}'")

    def update_page_by_title(self, title: str, html: str) -> Page:
        page = self.get_page_by_title(title)
        if page is None:
            logging.debug("Creating page '%s'", title)
            return self.course.create_page(wiki_page={ 'title': title
                                                     , 'body': html
                                                     })

        if page.body is None or not html_equiv(html, page.body):
            logging.debug("Updating page '%s'", title)
            page.edit(wiki_page={'body': html})

        return page

    def render_template(self, source: TextSource, template_vars: Optional[dict]=None) -> str:
        """Render a template using Jinja"""
        if isinstance(source, Path):
            with open(self.root / source, 'r', encoding='utf8') as f:
                text = f.read()
        else:
            text = source

        loader = FileSystemLoader(self.root)
        env = Environment(loader=loader)

        # Add filters
        env.filters["canvas_link"] = self.canvas_link

        # Add template variables
        env.globals.update(self.config.get('vars', {}))
        if template_vars is not None:
            env.globals.update(template_vars)

        # Create and render template
        template = env.from_string(text)

        return template.render(site=self.config['data'])

    def canvas_link(self, value):
        page = self.get_page_by_title(value)
        if page is None:
            return ""

        return page.html_url

    def render_markdown(self, source: TextSource, template_vars: Optional[dict]=None) -> str:
        """Render Markdown (using pandoc)"""
        jinja_text = self.render_template(source, template_vars)

        # Render using pandoc
        html = pypandoc.convert_text(jinja_text,
                                     to='html5+raw_html+smart',
                                     format='md',
                                     extra_args=['--mathjax'])

        # Upload all images
        soup = BeautifulSoup(html, 'lxml')

        for img in soup.find_all('img'):
            # Upload image file
            assert isinstance(source, Path)

            _, file = self.upload_file(self.root / source.parent / img['src'])

            # Replace image source with uploaded image
            file_url = f"/courses/{self.course.id:}/files/{file.id:}/preview"

            img['src'] = file_url

        return soup.prettify()

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

            self.sync_module(module, course_module, pred=make_filter(limits))

    def sync_syllabus(self, course: Course):
        """Synchronize course syllabus"""
        if 'syllabus' in self.config:
            logging.debug("Rendering syllabus")

            html = self.render_markdown(Path(self.config['syllabus']))

            if not html_equiv(course.syllabus_body, html):
                logging.debug("Updating course syllabus")
                course.update(course={'syllabus_body': html})

    def find_module_item(self, item: Any, idx: int, module_items: List[ModuleItem]) -> Optional[ModuleItem]:
        """Find a module item corresponding to an item dictionary.

        Args:
            item (Any): An item dictionary.
            idx (int): Item index.
            module_items (List[ModuleItem]): All module items.

        Returns:
            Optional[ModuleItem]: _description_
        """
        if len(module_items) > idx and module_items[idx].type == item_type(item):
            if module_items[idx].title == item['title']:
                return module_items[idx]

            if item_type(item) in ["SubHeader"]:
                return module_items[idx]

        for course_item in module_items:
             if course_item.type == item_type(item) and course_item.title == item['title']:
                 return course_item

        return None

    def sync_module(self, module: dict, course_module: Module, pred: Optional[Callable[[str], bool]]=None):
        if pred is not None and not pred(course_module.name):
            return

        logging.debug("Synchronizing module '%s'", module['name'])
        course_module.edit(module={ 'name': module['name']
                                  , 'published': module.get('published', False)
                                  })

        # Get all course module items
        course_module_items = list(course_module.get_module_items())

        # Flatten module items
        module_items = flatten_items(module['items'])

        # Sync module items
        for idx, item in enumerate(module_items):
            course_module_item = self.find_module_item(item, idx, course_module_items)
            logging.debug("Looking for %s", item['title'])

            if course_module_item is None:
                logging.debug("Not found, creating")
                course_module_item = self.create_module_item(course_module, item, idx)
                course_module_items.insert(idx, course_module_item)
                #course_module_items = list(course_module.get_module_items())
            else:
                logging.debug("Found %s", course_module_item.title)

            self.sync_module_item(course_module, item, idx, course_module_item)

        # Delete extra course items
        course_module_items = list(course_module.get_module_items())

        for course_module_item in course_module_items[len(module_items):]:
            logging.debug("Deleting extra item: %s", course_module_item)
            course_module_item.delete()

    def create_module_item(self, course_module: Module, item: dict, idx: int) -> ModuleItem:
        the_type = item_type(item)

        logging.debug("Creating module item (%s) '%s' at index %d", the_type, item['title'], idx)

        if the_type == "Page":
            html = self.render_markdown(get_page_source(item),
                                        template_vars=item.get('vars', None))

            page = self.update_page_by_title(item['title'], html)

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
            _, file = self.upload_file(filepath)

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

    def sync_module_item(self, course_module: Any, item: dict, idx: int, course_item: Any):
        the_type = item_type(item)

        logging.debug("Synchronizing '%s' (%s) with '%s' (%s)",
                      item['title'],
                      the_type,
                      course_item.title,
                      course_item.type)

        if the_type != course_item.type:
            raise ValueError(f"Cannot synchronize {the_type:} with {course_item.type:}")

        if the_type == "Page":
            html = self.render_markdown(get_page_source(item),
                                        template_vars=item.get('vars', None))
            page = self.update_page_by_title(item['title'], html)

            course_item.edit(module_item={ 'page_url': page.url })
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
                _, file = self.upload_file(filepath)

                file.update(hidden=not item.get('published', False))
        elif the_type == "Assignment":
            assignment = self.get_assignment(item['assignment'])

            if assignment is None:
                print(f"[red]Cannot find assignment {item['assignment']:}[/red]", file=sys.stderr)
                sys.exit(1)

            course_item.edit(module_item={ 'content_id': assignment.id })

            if 'description' in item:
                html = self.render_markdown(Path(item['description']),
                                            template_vars=item.get('vars', None))

                if assignment.description is None or not html_equiv(assignment.description, html):
                    assignment.edit(assignment={'description': html })

            for date_attr in ['due_at', 'lock_at', 'unlock_at']:
                if date_attr in item:
                    dt = parse_datetime(item[date_attr])
                    assignment.edit(assignment={date_attr: dt.isoformat()})
        else:
            raise ValueError(f"Can't handle item type {the_type:}")

        logging.debug("Updating %s '%s'", course_item.type, item['title'])
        if 'assignment' in item:
            title = item['assignment']
        else:
            title = item['title']

        course_item.edit(module_item={ 'title': title
                                     , 'indent': item['indent']
                                     , 'published': item.get('published', False)
                                     , 'position': idx+1
                                     })
