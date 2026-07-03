/**
 * Configuration file for CryptoViz frontend
 * 
 * This file contains global configuration settings for the application,
 * including API endpoints and other environment-specific variables.
 */

// Determine the API base URL based on the current environment
const API_BASE_URL = determineApiBaseUrl();

/**
 * Determines the appropriate API base URL based on the current environment
 *
 * The frontend is now served by the same Flask server that exposes the API,
 * so requests are made same-origin. Returning an empty string means fetches
 * like `${API_BASE_URL}/api/crypto` resolve to `/api/crypto` on the current host.
 *
 * @returns {string} The base URL for API requests
 */
function determineApiBaseUrl() {
    return '';
}
