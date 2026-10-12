"""Load the single shared route catalogue, fail closed on malformed metadata."""
import json
from pathlib import Path
import re

ANNUAL_TASKS = {f'Y{year}-{item:02d}' for year, count in ((1,4),(2,2),(3,6)) for item in range(1,count+1)}
SCENARIOS = {'normal','empty','error','denied','stale','unconnected'}
CATALOG = Path(__file__).resolve().parents[2] / 'frontend/preview/catalog.json'


def validate_catalog(pages: list[dict]) -> None:
    if not isinstance(pages, list) or not pages:
        raise ValueError('页面目录必须为非空列表')
    seen = set()
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError('无效页面')
        route = page.get('id', '')
        if not isinstance(route, str) or not re.fullmatch('[a-z][a-z-]*', route) or route in seen:
            raise ValueError('路由重复或无效')
        seen.add(route)
        if page.get('origin') not in {'demo','development','new'} or type(page.get('hidden')) is not bool:
            raise ValueError('来源或显示规则无效')
        if any(not isinstance(page.get(key),str) or not page[key].strip() for key in ('label','group','content_version')):
            raise ValueError('页面名称、分组或版本缺失')
        if not isinstance(page.get('annual_tasks'), list) or not page['annual_tasks'] or not set(page['annual_tasks']) <= ANNUAL_TASKS:
            raise ValueError('年度任务无效')
        if not isinstance(page.get('scenarios'),list) or not SCENARIOS <= set(page['scenarios']):
            raise ValueError('页面状态不完整')


def load_catalog(path: Path = CATALOG) -> list[dict]:
    pages = Path(path).read_text(encoding='utf-8')
    result = json.loads(pages)
    validate_catalog(result)
    return result
