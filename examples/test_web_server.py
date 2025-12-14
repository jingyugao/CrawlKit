"""Simple test web server for pagepool testing."""
# pyright: reportMissingImports=false

from flask import Flask, render_template_string, request, jsonify
import time
import random
import os

app = Flask(__name__)

# HTML template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ title }}</title>
    <meta charset="utf-8">
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 800px;
            margin: 50px auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
        }
        .info {
            background-color: #e3f2fd;
            padding: 15px;
            border-radius: 4px;
            margin: 20px 0;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin: 20px 0;
        }
        .stat-item {
            background-color: #f0f0f0;
            padding: 10px;
            border-radius: 4px;
        }
        .stat-label {
            font-weight: bold;
            color: #666;
        }
        .stat-value {
            color: #2196F3;
            font-size: 1.2em;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>{{ title }}</h1>
        <div class="info">
            <p><strong>Page ID:</strong> {{ page_id }}</p>
            <p><strong>Request Time:</strong> {{ timestamp }}</p>
            <p><strong>User Agent:</strong> {{ user_agent }}</p>
        </div>
        <div class="stats">
            <div class="stat-item">
                <div class="stat-label">Total Requests</div>
                <div class="stat-value">{{ total_requests }}</div>
            </div>
            <div class="stat-item">
                <div class="stat-label">Response Time</div>
                <div class="stat-value">{{ response_time }}ms</div>
            </div>
        </div>
        <p>This is a test page for pagepool crawler testing. Content changes on each request.</p>
        <p>Random content: {{ random_content }}</p>
    </div>
</body>
</html>
"""

# Request counter
request_counter = 0


@app.route('/')
def home():
    """Home page."""
    global request_counter
    request_counter += 1

    # Simulate variable response time
    delay = random.uniform(0.1, 0.5)
    time.sleep(delay)

    return render_template_string(
        HTML_TEMPLATE,
        title="Test Web Server - Home",
        page_id="home",
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        user_agent=request.headers.get('User-Agent', 'Unknown'),
        total_requests=request_counter,
        response_time=int(delay * 1000),
        random_content=f"Random number: {random.randint(1, 1000)}"
    )


@app.route('/page/<int:page_num>')
def page(page_num):
    """Dynamic page with page number."""
    global request_counter
    request_counter += 1

    # Simulate variable response time
    delay = random.uniform(0.1, 0.5)
    time.sleep(delay)

    return render_template_string(
        HTML_TEMPLATE,
        title=f"Test Page {page_num}",
        page_id=f"page-{page_num}",
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        user_agent=request.headers.get('User-Agent', 'Unknown'),
        total_requests=request_counter,
        response_time=int(delay * 1000),
        random_content=f"Page {page_num} - Random: {random.randint(1, 1000)}"
    )


@app.route('/slow')
def slow():
    """Slow page that takes time to load."""
    global request_counter
    request_counter += 1

    # Simulate slow response
    delay = random.uniform(2.0, 4.0)
    time.sleep(delay)

    return render_template_string(
        HTML_TEMPLATE,
        title="Slow Page",
        page_id="slow",
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        user_agent=request.headers.get('User-Agent', 'Unknown'),
        total_requests=request_counter,
        response_time=int(delay * 1000),
        random_content=f"This page loaded slowly! Random: {random.randint(1, 1000)}"
    )


@app.route('/api/stats')
def stats():
    """API endpoint returning stats."""
    return jsonify({
        'total_requests': request_counter,
        'timestamp': time.time(),
        'server': 'test-web-server',
        'version': '1.0.0'
    })


@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'healthy', 'requests': request_counter})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"Starting test web server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)
