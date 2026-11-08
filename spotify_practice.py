import spotipy
from spotipy.oauth2 import SpotifyOAuth

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    scope="user-read-playback-state user-modify-playback-state"
))

devices = sp.devices()["devices"]
print(f"Spotify devices found: {len(devices)}")
for d in devices:
    print(f"  - {d['name']} ({d['type']})")

current = sp.current_playback()
if current and current.get("item"):
    song = current["item"]
    print(f"Now playing: {song['name']} by {song['artists'][0]['name']}")
else:
    print("Nothing is playing right now.")
