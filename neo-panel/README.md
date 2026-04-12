# Neo Panel

A React Admin interface for monitoring Neo Core pipeline logs.

This application fetches and displays logs from each stage of the Neo Core pipeline in real-time.

## Features

- Real-time log monitoring for pipeline stages: validation, classification, external, build_prompt, generate_response, post_process
- Automatic polling every 2 seconds for updates
- Admin interface built with React Admin

## Getting Started

1. Install dependencies:
   ```bash
   npm install
   ```

2. Start the development server:
   ```bash
   npm run dev
   ```

3. Open your browser to `http://localhost:5173`

## API

The app connects to the Neo Core API at `http://localhost:8001` to fetch logs via `/logs/{stage}` endpoints.
