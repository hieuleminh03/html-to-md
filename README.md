# HTML to Markdown Converter - DigitalOcean Function

A DigitalOcean serverless function that converts HTML content to clean Markdown format with advanced preprocessing and table formatting.

## Features

- **Multiple Input Methods**: Accepts HTML via `Content-Type: text/html` or JSON payload
- **Advanced HTML Preprocessing**: Removes CSS, diagrams, scripts, and unwanted elements
- **Smart Table Handling**: Fixes broken markdown tables and handles nested tables
- **File Download Response**: Returns markdown as a downloadable `.md` file
- **Content Extraction**: Focuses on main content areas (Confluence-optimized)

## Prerequisites

- [DigitalOcean CLI (doctl)](https://docs.digitalocean.com/reference/doctl/how-to/install/) installed
- DigitalOcean account with Functions enabled
- Python 3.9 runtime support

## Installation & Deployment

### 1. Install doctl

```bash
# macOS
brew install doctl

# Linux (Arch-based)
pacman -S doctl

# Other Linux, Windows - Download from:
# https://github.com/digitalocean/doctl/releases
```

### 2. Authenticate with DigitalOcean

```bash
doctl auth init
```

### 3. Enable Functions (if not already enabled)

```bash
doctl serverless install
```

### 4. Deploy the Function

```bash
# Clone or download this project
git clone https://github.com/hieuleminh03/html-to-md
cd html-to-md

# Deploy the function
doctl serverless deploy . --remote-build

# (If you got problem with relative folder path above, use ./ instead of .)
```

### 5. Get Function URL

```bash
# List your functions to get the URL
doctl serverless functions list

# Or get the specific function details
doctl serverless functions get process/html
```

## Usage

### Method 1: Direct HTML Input (Recommended)

Send HTML content directly with `Content-Type: text/html`:

```bash
curl -X POST https://your-function-url \
  -H "Content-Type: text/html" \
  -d "<html><body><h1>Hello World</h1><p>This is a test.</p></body></html>" \
  --output converted.md
```

### Method 2: JSON Payload (Fallback)

Send HTML content wrapped in JSON:

```bash
curl -X POST https://your-function-url \
  -H "Content-Type: application/json" \
  -d '{"html": "<html><body><h1>Hello World</h1><p>This is a test.</p></body></html>"}' \
  --output converted.md
```

## Response

The function returns a downloadable markdown file with:
- **Content-Type**: `text/markdown`
- **Content-Disposition**: `attachment; filename="[title].md"`
- **Body**: Clean markdown content

## HTML Preprocessing

The function automatically cleans HTML content by:

### Content Extraction
- Extracts content from `<div id="content">` sections
- Removes content after `<div id="likes-and-labels-container">`

### Style Removal
- Removes CSS `<link>` tags
- Strips `<style>` blocks
- Eliminates inline `style` attributes

### Diagram & Media Cleanup
- Removes draw.io diagram containers
- Strips SVG and canvas elements
- Eliminates base64 image data URIs
- Removes diagram-related scripts

### Table Optimization
- Fixes broken markdown tables
- Handles nested table structures
- Normalizes cell content formatting

## Configuration

### Environment Variables
Edit `project.yml` to add environment variables:

```yaml
packages:
  - name: process
    environment:
      DEBUG: "false"
      MAX_CONTENT_SIZE: "10485760"  # 10MB
```

### Function Limits
Adjust limits in `project.yml`:

```yaml
actions:
  - name: html
    limits:
      timeout: 30000      # 30 seconds
      memory: 256         # 256MB
      logs: 10            # 10MB logs
```
