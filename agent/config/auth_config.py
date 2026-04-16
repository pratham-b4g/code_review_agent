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
DATABASE_URL = "postgresql://postgres:root@localhost:5432/postgres"
# .env file or environment variables
CRA_FLOW_URL="https://defaultcff20d814abd4f219998f39afd1df6.2a.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/243a9a7a866c46dca7f63ba89b2feced/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=YOxpQhyv1jIB2Cc2UDF7bX4PEXz0BTKb0Nnl2Kw7_RI"

# Example cloud URLs (uncomment and edit one of these):
# DATABASE_URL = "postgresql://user:pass@your-db-host.com:5432/cra_db"
# DATABASE_URL = "postgresql://user:pass@aws-rds-endpoint.region.rds.amazonaws.com:5432/cra_db"
# DATABASE_URL = "postgresql://user:pass@db.supabase.co:5432/postgres"
