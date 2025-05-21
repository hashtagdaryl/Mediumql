import os
import requests
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging # For better logging

# --- Flask App Setup ---
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO) # You can change this to logging.DEBUG for more verbose output
# If running in iSH/locally and not seeing logs from app.logger,
# this ensures Flask's default logger also outputs.
app.logger.setLevel(logging.INFO)


# CORS Configuration:
# For development in iSH and initial Render deployment, allow all origins.
# For a production frontend, you should restrict this to your actual frontend domain:
# Example: CORS(app, resources={r"/get-tag-feed": {"origins": "https://your-actual-frontend-domain.com"}})
CORS(app)

# --- Configuration ---
MEDIUM_GRAPHQL_ENDPOINT = "https://medium.com/_/graphql"
# IMPORTANT: Your Medium API Key will be set as an environment variable (MEDIUM_API_KEY)
MEDIUM_API_KEY = os.environ.get("MEDIUM_API_KEY")

# Headers that your script used.
# The User-Agent is good practice. Origin/Referer might be needed if Medium's API is strict.
BASE_REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json", # Explicitly request JSON
    "Origin": "https://medium.com",
    "Referer": "https://medium.com/",
    "User-Agent": "PythonBackend/1.0 (MediumFeedFetcher; +https://yourdomain.com/info)" # Be a good internet citizen
}

# --- Helper Functions ---
def clean_medium_response(text):
    """Cleans the typical Medium GraphQL response prefix like '])}while(1);</x>'."""
    prefix_to_remove = "])}while(1);</x>"
    if text.startswith(prefix_to_remove):
        return text[len(prefix_to_remove):]
    return text

# --- Routes ---
@app.route('/') # Root route to check if the app is alive
def home():
    app.logger.info("Home route accessed.")
    return jsonify({"status": "Medium TagFeed Proxy is running correctly!", "message": "Welcome!"}), 200

@app.route('/get-tag-feed', methods=['POST'])
def get_tag_feed_handler():
    app.logger.info(f"Received request for /get-tag-feed from IP: {request.remote_addr}")

    if not MEDIUM_API_KEY:
        app.logger.error("CRITICAL: MEDIUM_API_KEY environment variable not configured on the server.")
        return jsonify({"error": "Server configuration error: API key missing."}), 500

    try:
        client_data = request.get_json()
        if not client_data:
            app.logger.warning("Received empty/invalid JSON data in request body.")
            return jsonify({"error": "No JSON data provided in request body or invalid JSON format."}), 400
    except Exception as e:
        app.logger.warning(f"Error decoding JSON from request: {e}")
        return jsonify({"error": "Invalid JSON format in request body."}), 400


    tag_slug = client_data.get('tagSlug')
    mode = client_data.get('mode')

    if not tag_slug:
        app.logger.info("Request missing 'tagSlug'.")
        return jsonify({"error": "Missing 'tagSlug' in request body."}), 400
    if not mode:
        app.logger.info("Request missing 'mode'.")
        return jsonify({"error": "Missing 'mode' in request body."}), 400

    mode = str(mode).strip().upper() # Ensure mode is a string before upper()
    valid_modes = ["HOT", "NEW", "TOP_ALL_TIME", "TOP_MONTH", "TOP_WEEK", "TOP_YEAR"]
    if mode not in valid_modes:
        app.logger.info(f"Invalid mode '{mode}' provided.")
        return jsonify({"error": f"Invalid mode. Must be one of: {', '.join(valid_modes)}"}), 400

    app.logger.info(f"Processing TagFeed request: tagSlug='{tag_slug}', mode='{mode}'")

    graphql_query = """
    query TagFeed($tagSlug: String!, $mode: TagFeedMode!) {
      tagFeed(tagSlug: $tagSlug, mode: $mode) {
        items {
          feedId
          post {
            id # Post ID, often useful
            title
            mediumUrl # Canonical URL if available
            uniqueSlug # Used for constructing URLs
            creator {
              id # User ID
              name
              username # Often part of the URL structure
            }
          }
        }
      }
    }
    """

    variables = {
        "tagSlug": tag_slug,
        "mode": mode
    }

    payload = {
        "query": graphql_query,
        "variables": variables
    }

    # Prepare request headers for Medium API call
    request_headers_to_medium = {**BASE_REQUEST_HEADERS} # Make a copy
    # Ensure this matches Medium's expected auth scheme for its GraphQL API key
    # Common schemes: "Bearer {token}", "apikey {token}", or custom like "x-api-key: {token}"
    request_headers_to_medium["Authorization"] = f"Bearer {MEDIUM_API_KEY}"
    # Example for a different header:
    # request_headers_to_medium["X-YOUR-API-KEY-HEADER"] = MEDIUM_API_KEY

    app.logger.debug(f"Sending GraphQL request to Medium. Payload: {json.dumps(payload, indent=2)}")

    try:
        response_from_medium = requests.post(
            MEDIUM_GRAPHQL_ENDPOINT,
            headers=request_headers_to_medium,
            json=payload,
            timeout=25 # Slightly longer timeout, adjust as needed
        )
        # This will raise an HTTPError if the HTTP request returned an unsuccessful status code (4xx or 5xx)
        response_from_medium.raise_for_status()

        cleaned_text = clean_medium_response(response_from_medium.text)
        data_from_medium = json.loads(cleaned_text)

        # Check for GraphQL-specific errors returned in the JSON body (even with a 200 OK HTTP status)
        if "errors" in data_from_medium and data_from_medium["errors"]:
            app.logger.warning(f"GraphQL errors received from Medium: {json.dumps(data_from_medium['errors'], indent=2)}")
            # You might want to return these specific errors to the client
            return jsonify({"error": "GraphQL error(s) received from Medium.", "details": data_from_medium["errors"]}), 400 # Or 502 if it's a server-side issue with Medium

        # Process and format the successful response
        processed_articles = []
        items = data_from_medium.get("data", {}).get("tagFeed", {}).get("items", [])
        app.logger.info(f"Received {len(items)} items from Medium for tag '{tag_slug}'.")

        for item_index, item in enumerate(items):
            post_data = item.get("post")
            if not post_data:
                app.logger.debug(f"Item {item_index} skipped: no 'post' object.")
                continue

            feed_id = item.get("feedId") # May or may not be the same as post.id
            title = post_data.get("title", "Untitled")
            author_name = post_data.get("creator", {}).get("name", "Unknown Author")
            
            article_link = post_data.get("mediumUrl") # Prefer this if available

            if not article_link: # Fallback link construction
                unique_slug = post_data.get("uniqueSlug")
                author_username = post_data.get("creator", {}).get("username")
                if unique_slug:
                    if author_username:
                        article_link = f"https://{author_username}.medium.com/{unique_slug}"
                    else:
                        # Fallback if author username isn't available for the URL pattern
                        # This pattern might need adjustment based on how Medium constructs URLs for posts without a custom subdomain
                        article_link = f"https://medium.com/p/{post_data.get('id', unique_slug)}" # Use post.id if available, else uniqueSlug
                elif feed_id: # Absolute last resort, may not be a direct link
                    article_link = f"https://medium.com/p/{feed_id}" # This format can be unreliable; test it
            
            if not article_link:
                article_link = f"Link construction failed (feedId: {feed_id}, postId: {post_data.get('id')})"


            processed_articles.append({
                "title": title,
                "author": author_name,
                "link": article_link
            })

        app.logger.info(f"Successfully processed {len(processed_articles)} articles.")
        return jsonify({"articles": processed_articles}), 200

    except requests.exceptions.HTTPError as e:
        # This catches errors from response_from_medium.raise_for_status() (4xx/5xx from Medium)
        error_message = f"HTTPError contacting Medium: Status {e.response.status_code}"
        try:
            medium_error_details = e.response.json()
            error_message += f" - Details: {json.dumps(medium_error_details, indent=2)}"
        except json.JSONDecodeError:
            error_message += f" - Response: {e.response.text[:500]}" # Show start of non-JSON error response
        app.logger.error(error_message)
        return jsonify({"error": "Failed to communicate effectively with Medium API.", "details": str(e)}), e.response.status_code if e.response is not None else 503
    except requests.exceptions.Timeout:
        app.logger.error(f"Timeout while trying to contact Medium API at {MEDIUM_GRAPHQL_ENDPOINT}")
        return jsonify({"error": "Request to Medium API timed out.", "details": "The upstream server took too long to respond."}), 504 # Gateway Timeout
    except requests.exceptions.RequestException as e:
        # This catches other network issues like DNS failure, connection refused, etc.
        app.logger.error(f"RequestException (Network issue) while contacting Medium: {str(e)}")
        return jsonify({"error": "Network error connecting to Medium API. Check internet connection.", "details": str(e)}), 503 # Service Unavailable
    except json.JSONDecodeError as e:
        # This catches errors if Medium's response (after cleaning) isn't valid JSON
        # It's good to log the problematic text if possible (be careful with large responses)
        raw_text = response_from_medium.text if 'response_from_medium' in locals() else "Response text unavailable"
        app.logger.error(f"Failed to decode JSON response from Medium: {str(e)}. Response text sample: {raw_text[:500]}")
        return jsonify({"error": "Invalid or unexpected response format from Medium.", "details": str(e)}), 502 # Bad Gateway
    except Exception as e:
        # Catch-all for any other unexpected errors in this route
        app.logger.critical(f"An critical unexpected error occurred in get_tag_feed_handler: {type(e).__name__} - {str(e)}", exc_info=True)
        # exc_info=True in logger.critical will log the full traceback
        return jsonify({"error": "An unexpected internal server error occurred.", "details": "Please check server logs."}), 500

# --- Main Execution Block ---
if __name__ == '__main__':
    # For local development (iSH or your computer). Render will use Gunicorn.
    # Using host='0.0.0.0' makes the server accessible from other devices on your local network (e.g., your computer accessing iSH).
    # Debug mode is useful for development as it provides detailed error pages and auto-reloads on code changes.
    # Do not run with debug=True in a production environment deployed to the public. Render will handle this.
    
    # Get PORT from environment, default to 8080 (common for dev)
    # Render sets its own PORT environment variable.
    port = int(os.environ.get("PORT", 8080))
    
    app.logger.info(f"Starting Flask development server on host 0.0.0.0, port {port}, debug=True")
    app.run(host='0.0.0.0', port=port, debug=False)
