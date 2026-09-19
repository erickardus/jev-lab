def test_signup_creates_user(client, db):
    r = client.post("/signup", json={"email": "jane.doe@example.com", "name": "Jane Doe", "national_id": "123-45-6789"})
    assert r.status_code == 200
