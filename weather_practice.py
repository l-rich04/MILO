import requests

url = "https://api.open-meteo.com/v1/forecast"
params = {
    "latitude": 40.33,
    "longitude": -78.92,
    "current": "temperature_2m,weather_code",
    "temperature_unit": "fahrenheit",
}

response = requests.get(url, params=params, timeout=10)
data = response.json()

temp = data["current"]["temperature_2m"]
print(f"It's {temp}°F right now")

print(f"The weather code is {data['current']['weather_code']}")
