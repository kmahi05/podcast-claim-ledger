import uuid

from app import storage


async def test_media_signed_range(client):
    rel = storage.object_path(uuid.uuid4(), uuid.uuid4(), "mp3")
    path = storage.absolute(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(range(256)) * 4)

    url = storage.signed_url(rel)
    r = await client.get(url, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == bytes(range(10, 20))
    assert r.headers["content-type"] == "audio/mpeg"

    assert (await client.get(url)).status_code == 200
    assert (await client.get(url[:-1] + ("0" if url[-1] != "0" else "1"))).status_code == 403
    bad = f"/api/v1/media/not-a-uuid/{uuid.uuid4()}.mp3?exp=9999999999&sig=00"
    assert (await client.get(bad)).status_code == 404
