# Centralized Authentication Database Guide

This project is configured to support a **Centralized Authentication Database** (Single Sign-On / Shared Identity approach). This allows you to use a separate MongoDB database dedicated solely to storing User Credentials (username, email, password, and shared roles), while keeping the main application data (links, vouchers, etc.) in the project's primary database.

By doing this, you can connect multiple different platforms or applications to the same `AUTH_MONGODB_URI`. All platforms will then share the same pool of users, meaning if a user creates an account on one platform, they can log into any other platform using the exact same email and password (provided they share the same `JWT_SECRET`).

## Facts about the shared identity store (verified against KeywordSearch)

- The accounts live in a collection named **`User Accounts`** (note the space and capitals — set in `Backend/models/User.js` as `collection: 'User Accounts'`).
- `email` is stored lowercased; `password` is a bcrypt hash (bcryptjs, salt rounds 10, `$2a$` prefix); `name` and `role` (`user`/`admin`) are plain strings.
- JWTs are signed HS256 with the shared `JWT_SECRET` and carry a **`userId`** claim (the `_id` string): `jsonwebtoken.sign({ userId }, JWT_SECRET, { expiresIn: '7d' })`. Verifiers look the user up with `User.findById(decoded.userId)`.
- App-specific data (e.g. credits) is nested under `apps.<appName>` on the user document, never at the root.

Python consumers (like Nestick) honor this in `nestick/auth.py`: the collection is configurable via `AUTH_MONGODB_COLLECTION` (default `User Accounts`), and tokens issued by `issue_token()` include `userId` so Node platforms accept them.

## Environment Variables Setup

In your backend's root directory, locate or create your `.env` file. You need to define two separate MongoDB URIs:

```env
# 1. Main Database (Specific to this app's data: searches, vouchers, etc.)
MONGODB_URI=mongodb+srv://<username>:<password>@cluster0.example.mongodb.net/MainAppDB

# 2. Central Authentication Database (Shared across all your apps)
AUTH_MONGODB_URI=mongodb+srv://<username>:<password>@cluster0.example.mongodb.net/CentralAuthDB
```

### How It Works

1. **When `AUTH_MONGODB_URI` is provided:** 
   The application will automatically connect to this secondary database to read, write, and verify the `User` model. The `MONGODB_URI` will be used for everything else.

2. **When `AUTH_MONGODB_URI` is missing:**
   The application is built with a fallback mechanism. It will gracefully default back to saving `User` data inside the standard `MONGODB_URI`. 

## How to Integrate with Other Platforms

To add this shared authentication system to any of your other Node.js/Mongoose platforms:

1. Copy the `AUTH_MONGODB_URI` to the `.env` file of your other platform.
2. Ensure your other platform creates a separate Mongoose connection to this URI using `mongoose.createConnection(process.env.AUTH_MONGODB_URI)`.
3. Ensure the other platform registers its `User` model specifically onto that connection, rather than the default `mongoose` object. 

*Example for connecting on another platform:*
```javascript
const mongoose = require('mongoose');

// Create the connection
const authDBConnection = mongoose.createConnection(process.env.AUTH_MONGODB_URI);

// Register the User model onto this connection
const User = authDBConnection.model('User', userSchema);

module.exports = User;
```

## App-Specific Data (e.g. Credits)

Since multiple apps share the same `User` database, adding fields directly to the root of the User schema (e.g., `credits: Number`) causes conflicts between apps. 

To solve this, app-specific data **must be nested under the `apps` dictionary** in the User schema. 

For example, KeywordSearch stores its credits under `apps.keywordSearch.credits`. When you build a new app, add a new block to the schema:

```javascript
const userSchema = new mongoose.Schema({
  // Shared fields...
  name: String,
  email: String,
  
  // App-specific data
  apps: {
    keywordSearch: {
      credits: { type: Number, default: 0 }
    },
    yourNewApp: {
      credits: { type: Number, default: 10 },
      themePreference: { type: String, default: 'dark' }
    }
  }
});
```

## Cross-Database Population (Important)

When an entity in your Main Database (like a `SearchLink`) references a `User` from the Auth Database, **do not use Mongoose's `.populate('createdBy')`**. Cross-database population via Mongoose is fragile, slow, and hard to maintain.

Instead, we use a Redis-backed caching layer to perform the lookups. When you need to attach User data to an array of results, map over them and use the shared `getUserCached(userId)` service:

```javascript
const { getUserCached } = require('../services/userService');

// Get raw documents from Main DB
const rawLinks = await SearchLink.find().lean();

// Map and lookup users via Redis/Auth DB
const links = await Promise.all(rawLinks.map(async (link) => ({
  ...link,
  createdBy: await getUserCached(link.createdBy) || link.createdBy
})));
```

This ensures fast performance (Redis caches the `name/email/role` for 5 minutes) and keeps your database queries decoupled!
