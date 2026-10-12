"""Application-wide constants."""

# Version info
APP_NAME = "Claude Code Telegram Bot"
APP_DESCRIPTION = "Telegram bot for remote Claude Code access"

# Default limits
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 300
DEFAULT_CLAUDE_MAX_TURNS = 10
DEFAULT_CLAUDE_MAX_COST_PER_USER = 10.0
DEFAULT_CLAUDE_MAX_COST_PER_USER_GUEST = 2.0
DEFAULT_CLAUDE_MAX_COST_PER_REQUEST = 5.0

DEFAULT_RATE_LIMIT_REQUESTS = 10
DEFAULT_RATE_LIMIT_WINDOW = 60
DEFAULT_RATE_LIMIT_BURST = 20
DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS = 1.1

# Session watchdog (dv-8bb5): soft, hourly thresholds that fire an admin
# alert rather than blocking. Generous on purpose -- a false positive just
# costs the admin one glance and a dismiss, a missed one lets a runaway
# session keep spending. Tune from real usage once it's shipped.
DEFAULT_WATCHDOG_MAX_COST_PER_HOUR = 20.0
DEFAULT_WATCHDOG_MAX_MESSAGES_PER_HOUR = 60
DEFAULT_WATCHDOG_MAX_TURNS_PER_HOUR = 100

# Unmetered backends (dv-0722): a Codex turn on a ChatGPT subscription has no
# knowable USD price, so those turns are budgeted in tokens. The defaults are
# the USD caps above converted at a blended ~$4/Mtok (mostly-input turn at
# Sonnet-class rates), so a user on Codex gets roughly the same allowance as
# the same user on Claude. Per-turn input is the full context re-sent, so a
# 100k-context conversation burns ~100k per turn.
DEFAULT_UNMETERED_MAX_TOKENS_PER_USER = 2_500_000
DEFAULT_UNMETERED_MAX_TOKENS_PER_USER_GUEST = 500_000
DEFAULT_WATCHDOG_MAX_TOKENS_PER_HOUR = 5_000_000

DEFAULT_SESSION_TIMEOUT_HOURS = 24
DEFAULT_MAX_SESSIONS_PER_USER = 5

# Message limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
SAFE_MESSAGE_LENGTH = 4000  # Leave room for formatting

# Session limits
MAX_SESSION_LENGTH = 1000  # Maximum messages per session

# File limits
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Allowed file extensions
ALLOWED_FILE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".sh",
    ".bash",
}

# Security patterns to block
DANGEROUS_PATTERNS = [
    r"\.\.",  # Parent directory
    r"~",  # Home directory
    r"\$",  # Variable expansion
    r"`",  # Command substitution
    r";",  # Command chaining
    r"&&",  # Command chaining
    r"\|\|",  # Command chaining
    r">",  # Redirection
    r"<",  # Redirection
    r"\|",  # Piping
]

# Database defaults
DEFAULT_DATABASE_URL = "sqlite:///data/bot.db"
# Per-user data store (dv-5f3a): logs/products/portions/issues/docs live in
# their own file; bot.db stays analytic/monitoring.
DEFAULT_USER_DATABASE_URL = "sqlite:///data/data.db"
DEFAULT_BACKUP_RETENTION_DAYS = 30

# Claude Code defaults
DEFAULT_CLAUDE_BINARY = "claude"
DEFAULT_CLAUDE_OUTPUT_FORMAT = "stream-json"

# Soul & Memory defaults
DEFAULT_MEMORY_PATH = "data/memory"
DEFAULT_SOUL_MD_PATH = "data/soul.md"

# First line of the shipped default persona (data/templates/agent.md) until the
# owner configures one (dv-c6be). The onboarding gate (dv-bf1e) checks the
# default profile's persona for it; rewriting the persona removes it.
PERSONA_UNCONFIGURED_MARKER = "<!-- devvy:unconfigured -->"
DEFAULT_ACTIVITY_LOG_MAX_LINES = 200

# Logging
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
