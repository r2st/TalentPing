#!/usr/bin/env bash
# TalentPing — setup and end-to-end test script
# Run from repo root: bash setup_and_test.sh
set -euo pipefail
cd "$(dirname "$0")"

API=http://localhost:8000/api/v1

echo "══════════════════════════════════════════"
echo "  Step 1: Docker Compose (build + start)"
echo "══════════════════════════════════════════"
docker compose up -d --build
echo "Waiting for services to be healthy..."
sleep 5

# Wait for API to respond
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/ >/dev/null 2>&1; then
    echo "API is up!"
    break
  fi
  echo "  waiting... ($i/30)"
  sleep 2
done

echo ""
echo "══════════════════════════════════════════"
echo "  Step 2: Run Alembic migrations"
echo "══════════════════════════════════════════"
docker compose exec api alembic upgrade head
echo "Migrations complete."

echo ""
echo "══════════════════════════════════════════"
echo "  Step 3: End-to-end API test"
echo "══════════════════════════════════════════"

echo ""
echo "--- 3a: Register user ---"
REG=$(curl -sf -X POST "$API/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@talentping.dev","password":"SecurePass123!","full_name":"Suman Das"}')
echo "$REG" | python3 -m json.tool
USER_ID=$(echo "$REG" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "User ID: $USER_ID"

echo ""
echo "--- 3b: Login ---"
LOGIN=$(curl -sf -X POST "$API/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=test@talentping.dev&password=SecurePass123!")
TOKEN=$(echo "$LOGIN" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "Got JWT token: ${TOKEN:0:20}..."
AUTH="Authorization: Bearer $TOKEN"

echo ""
echo "--- 3c: Create profile ---"
PROFILE=$(curl -sf -X PUT "$API/profile" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{
    "headline":"Senior Software Engineer",
    "years_experience":8,
    "location":"San Francisco, CA",
    "skills":["Python","FastAPI","PostgreSQL","React","AWS"],
    "target_roles":["Staff Engineer","Engineering Manager"],
    "target_industries":["fintech","AI/ML"]
  }')
echo "$PROFILE" | python3 -m json.tool

echo ""
echo "--- 3d: Add recruiter ---"
REC=$(curl -sf -X POST "$API/recruiters" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{
    "name":"Sarah Chen",
    "email":"sarah.chen@techrecruit.example.com",
    "company":"TechRecruit Partners",
    "title":"Senior Technical Recruiter",
    "industry":"Technology",
    "specialization":"Backend & Infrastructure Engineering"
  }')
echo "$REC" | python3 -m json.tool
REC_ID=$(echo "$REC" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Recruiter ID: $REC_ID"

echo ""
echo "--- 3e: Create campaign ---"
CAMP=$(curl -sf -X POST "$API/campaigns" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{
    "name":"Q3 2026 Outreach",
    "description":"Targeted outreach to tech recruiters",
    "target_roles":["Staff Engineer"],
    "target_industries":["fintech","AI/ML"]
  }')
echo "$CAMP" | python3 -m json.tool
CAMP_ID=$(echo "$CAMP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Campaign ID: $CAMP_ID"

echo ""
echo "--- 3f: Generate AI email draft ---"
GEN=$(curl -sf -X POST "$API/campaigns/$CAMP_ID/generate" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d "{\"recruiter_ids\":[$REC_ID]}")
echo "$GEN" | python3 -m json.tool

echo ""
echo "══════════════════════════════════════════"
echo "  Step 4: Run test suite (inside container)"
echo "══════════════════════════════════════════"
docker compose exec api python -m pytest tests/ -v

echo ""
echo "══════════════════════════════════════════"
echo "  ALL DONE"
echo "══════════════════════════════════════════"
echo ""
echo "Stack status:"
docker compose ps
