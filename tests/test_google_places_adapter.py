from app.adapters.google_places import normalize_place_details, normalize_text_search


def test_google_places_text_search_normalization():
    raw = {
        "places": [
            {
                "id": "places/abc",
                "displayName": {"text": "Source Hotel"},
                "formattedAddress": "1 Main St",
                "rating": 4.3,
                "userRatingCount": 120,
                "googleMapsUri": "https://maps.google.test/place",
                "websiteUri": "https://hotel.example",
            }
        ]
    }

    normalized = normalize_text_search(raw)

    place = normalized["places"][0]
    assert place["source_name"] == "google_places"
    assert place["place_id"] == "places/abc"
    assert place["display_name"] == "Source Hotel"
    assert place["formatted_address"] == "1 Main St"
    assert place["rating"] == 4.3
    assert place["user_rating_count"] == 120
    assert place["google_maps_uri"] == "https://maps.google.test/place"
    assert place["website_uri"] == "https://hotel.example"


def test_google_places_details_normalization():
    raw = {
        "id": "places/abc",
        "displayName": {"text": "Source Hotel"},
        "formattedAddress": "1 Main St",
        "rating": 4.3,
        "userRatingCount": 120,
    }

    normalized = normalize_place_details(raw)

    assert normalized["place"]["place_id"] == "places/abc"
    assert normalized["place"]["display_name"] == "Source Hotel"
