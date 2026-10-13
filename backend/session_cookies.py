"""Select one installation's session without falling back to another UI's cookie."""


def cookie_name(server) -> str:
    return 'oilwell_monitoring_session' if getattr(server, 'ui_mode', 'standard') == 'monitoring' else 'platform_session'


def read_session_cookie(headers, name: str) -> str | None:
    tokens = []
    for line in headers.get_all('Cookie', []):
        for part in line.split(';'):
            key, separator, value = part.strip().partition('=')
            if separator and key == name:
                tokens.append(value)
    return tokens[0] if len(tokens) == 1 and tokens[0] else None
