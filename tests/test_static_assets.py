import http.client
from pathlib import Path
import tempfile
import threading
import unittest

from backend.http_server import create_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StaticShellContractTests(unittest.TestCase):
    def test_index_uses_module_entry_and_accessible_regions(self):
        html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('type="module" src="./app.js?v=', html)
        self.assertIn('aria-label="一级功能导航"', html)
        self.assertIn('id="workspace"', html)
        self.assertIn('aria-live="polite"', html)

    def test_shared_frontend_modules_expose_required_interfaces(self):
        expected = {
            "frontend/api.js": ("export const api", "request("),
            "frontend/store.js": ("export const store", "subscribe("),
            "frontend/router.js": ("export const router", "register(", "restore("),
            "frontend/components.js": (
                "export const components", "table(", "pagination(", "modal(",
                "toast(", "formField(", "statusTag(", "skeleton(", "errorPanel(",
            ),
            "frontend/format.js": ("export function",),
            "frontend/pages/login.js": ("export async function renderLogin",),
        }
        for relative, markers in expected.items():
            with self.subTest(relative=relative):
                source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                for marker in markers:
                    self.assertIn(marker, source)

    def test_visible_shell_copy_has_no_inspection_only_labels(self):
        paths = [PROJECT_ROOT / "index.html", PROJECT_ROOT / "app.js"]
        frontend = PROJECT_ROOT / "frontend"
        if frontend.exists():
            paths.extend(frontend.rglob("*.js"))
        combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        for forbidden in ("演示系统", "演示账号", "演示数据"):
            self.assertNotIn(forbidden, combined)

    def test_shell_maps_real_roles_and_has_global_error_boundary(self):
        source = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")
        for role in (
            "administrator", "production_engineer", "algorithm_engineer",
            "maintenance_engineer", "reviewer",
        ):
            self.assertIn(f'{role}:', source)
        self.assertIn('window.addEventListener("error"', source)
        self.assertIn('window.addEventListener("unhandledrejection"', source)


class StaticServerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(
            "127.0.0.1", 0,
            database_path=Path(self.temporary.name) / "platform.db",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)

    def _close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _get(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_static_server_rejects_path_traversal(self):
        status, _, body = self._get("/../backend/database.py")
        self.assertEqual(status, 404)
        self.assertNotIn(b"class Database", body)

    def test_frontend_module_is_served_as_javascript(self):
        status, headers, body = self._get("/frontend/api.js")
        self.assertEqual(status, 200)
        self.assertIn(b"export const api", body)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
