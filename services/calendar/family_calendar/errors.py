class CalendarError(Exception):
    """Public fixed error code; never include provider payloads or credentials.

    `field_errors` names the input fields at fault, as [{"field": path, "reason": fixed word}].
    `details` carries fixed, owner-facing text for local CLI errors only.
    """

    def __init__(self, code: str, field_errors: list | None = None, details: dict | None = None):
        self.code = code
        self.field_errors = field_errors or []
        self.details = details or {}
        super().__init__(code)
