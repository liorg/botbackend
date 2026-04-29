import os

print("=== Debugging Supabase Environment Variables ===\n")

url = os.getenv("SUPABASE_URL")
service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
anon_key = os.getenv("SUPABASE_KEY")

print(f"SUPABASE_URL                  : {'✅ Loaded' if url else '❌ Missing'}")
print(f"SUPABASE_SERVICE_ROLE_KEY     : {'✅ Loaded' if service_key else '❌ Missing'}")
print(f"SUPABASE_KEY (fallback)       : {'✅ Loaded' if anon_key else '❌ Missing'}")

print("\n--- Values (first 10 chars only for security) ---")
print(f"URL     : {url[:10]}..." if url else "URL: None")
print(f"Service Key : {service_key[:10]}..." if service_key else "Service Key: None")