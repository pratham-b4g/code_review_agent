"""Super Admin authentication configuration.

IMPORTANT: Edit these credentials before deploying!
The super admin has full access to all projects, users, and settings.
"""

# Super Admin Credentials
# Change these to your actual email and a strong password
SUPER_ADMIN_EMAIL = "admin@example.com"
SUPER_ADMIN_PASSWORD = "admin123"

# Database URL (PostgreSQL)
# Default: Local PostgreSQL
# Change this to your cloud PostgreSQL URL
# DATABASE_URL = "postgresql://cra_user:cra_pass@localhost:5432/cra_db"
DATABASE_URL = "postgresql://postgres:root@localhost:5432/cra"

# Example cloud URLs (uncomment and edit one of these):
# DATABASE_URL = "postgresql://user:pass@your-db-host.com:5432/cra_db"
# DATABASE_URL = "postgresql://user:pass@aws-rds-endpoint.region.rds.amazonaws.com:5432/cra_db"
# DATABASE_URL = "postgresql://user:pass@db.supabase.co:5432/postgres"
