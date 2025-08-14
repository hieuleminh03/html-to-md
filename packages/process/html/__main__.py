import json
import sys
import os
import re
import io
import base64
from typing import Any, Optional, List
from urllib.parse import quote, unquote, urlparse, urlunparse

try:
    import markdownify
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"Missing required dependency: {e}")
    print("Please install required packages:")
    print("pip install markdownify beautifulsoup4")
    sys.exit(1)


class DocumentConverterResult:
    """The result of converting a document to Markdown."""

    def __init__(self, markdown: str, *, title: Optional[str] = None):
        self.markdown = markdown
        self.title = title

    @property
    def text_content(self) -> str:
        return self.markdown

    def __str__(self) -> str:
        return self.markdown


class _CustomMarkdownify(markdownify.MarkdownConverter):
    """
    A custom version of markdownify's MarkdownConverter. Changes include:

    - Altering the default heading style to use '#', '##', etc.
    - Removing javascript hyperlinks.
    - Truncating images with large data:uri sources.
    - Ensuring URIs are properly escaped, and do not conflict with Markdown syntax
    - Preserving nested HTML tables by falling back to raw HTML when needed
    - Optional sanitization/normalization of HTML tables to reduce noise
    """

    def __init__(self, **options: Any):
        options["heading_style"] = options.get("heading_style", markdownify.ATX)
        options["keep_data_uris"] = options.get("keep_data_uris", False)
        # Preserve tables as raw HTML when requested (useful for Confluence/nested tables)
        options["preserve_tables_as_html"] = options.get("preserve_tables_as_html", False)
        # Sanitize HTML tables when preserving them as HTML (strip classes/inline styles, keep essential attrs)
        options["sanitize_tables_html"] = options.get("sanitize_tables_html", True)
        # Strategy for nested tables: 'append' (outer to md, nested appended) or 'html'
        options["nested_table_handling"] = options.get("nested_table_handling", "append")
        # Explicitly cast options to the expected type if necessary
        super().__init__(**options)

    def convert_hn(
        self,
        n: int,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """Same as usual, but be sure to start with a new line"""
        if not convert_as_inline:
            if not re.search(r"^\n", text):
                return "\n" + super().convert_hn(n, el, text, convert_as_inline)  # type: ignore

        return super().convert_hn(n, el, text, convert_as_inline)  # type: ignore

    def convert_a(
        self,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ):
        """Same as usual converter, but removes Javascript links and escapes URIs."""
        prefix, suffix, text = markdownify.chomp(text)  # type: ignore
        if not text:
            return ""

        if el.find_parent("pre") is not None:
            return text

        href = el.get("href")
        title = el.get("title")

        # Escape URIs and skip non-http or file schemes
        if href:
            try:
                parsed_url = urlparse(href)  # type: ignore
                if parsed_url.scheme and parsed_url.scheme.lower() not in ["http", "https", "file"]:  # type: ignore
                    return "%s%s%s" % (prefix, text, suffix)
                href = urlunparse(parsed_url._replace(path=quote(unquote(parsed_url.path))))  # type: ignore
            except ValueError:  # It's not clear if this ever gets thrown
                return "%s%s%s" % (prefix, text, suffix)

        # For the replacement see #29: text nodes underscores are escaped
        if (
            self.options["autolinks"]
            and text.replace(r"\_", "_") == href
            and not title
            and not self.options["default_title"]
        ):
            # Shortcut syntax
            return "<%s>" % href
        if self.options["default_title"] and not title:
            title = href
        title_part = ' "%s"' % title.replace('"', r"\"") if title else ""
        return (
            "%s[%s](%s%s)%s" % (prefix, text, href, title_part, suffix)
            if href
            else text
        )

    def convert_img(
        self,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """Same as usual converter, but removes data URIs"""

        alt = el.attrs.get("alt", None) or ""
        src = el.attrs.get("src", None) or ""
        title = el.attrs.get("title", None) or ""
        title_part = ' "%s"' % title.replace('"', r"\"") if title else ""
        if (
            convert_as_inline
            and el.parent.name not in self.options["keep_inline_images_in"]
        ):
            return alt

        # Remove dataURIs
        if src.startswith("data:") and not self.options["keep_data_uris"]:
            src = src.split(",")[0] + "..."

        return "![%s](%s%s)" % (alt, src, title_part)

    def convert_table(
        self,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """Smarter table handling with nested tables support.

        Behavior:
        - If preserve_tables_as_html is True: always return sanitized raw HTML tables.
        - If table contains nested <table>:
            * If nested_table_handling == 'html': return sanitized raw HTML for the whole table.
            * If nested_table_handling == 'append' (default): render the outer table as Markdown; replace cells with text-only content; append nested tables below the table (converted to Markdown when simple, or sanitized HTML otherwise), and insert reference markers in affected cells.
        - Else: use markdownify's default table conversion (pipe tables).
        """
        try:
            # Ensure el is a BeautifulSoup Tag
            if not hasattr(el, 'find') and isinstance(el, str):
                el = BeautifulSoup(el, 'html.parser')

            if self.options.get("preserve_tables_as_html"):
                return "\n\n" + self._sanitize_table_html(str(el)) + "\n\n"

            has_nested = hasattr(el, 'find') and el.find("table") is not None
            if has_nested:
                handling = self.options.get("nested_table_handling", "append")
                if handling == 'html':
                    return "\n\n" + self._sanitize_table_html(str(el)) + "\n\n"

                # 'append' strategy: outer to Markdown, nested appended below as separate markdown tables
                nested_idx = 1

                def escape_pipes(s: str) -> str:
                    return s.replace('|', r'\|')

                def text_without_nested(cell):
                    # Clone cell and remove nested tables before extracting text
                    cell_copy = BeautifulSoup(str(cell), 'html.parser')
                    for t in cell_copy.find_all('table'):
                        t.decompose()
                    return cell_copy.get_text(" ", strip=True)

                def convert_nested_table_to_markdown(table_tag):
                    """Convert a nested table to markdown format"""
                    try:
                        # Check if this nested table has further nested tables
                        has_nested_nested = table_tag.find("table") is not None
                        if has_nested_nested:
                            # If nested table has its own nested tables, fall back to HTML
                            return self._sanitize_table_html(str(table_tag))
                        
                        # Manual conversion to markdown table format
                        nested_rows = []
                        
                        # Get all rows from the nested table
                        thead = table_tag.find('thead')
                        tbody = table_tag.find('tbody')
                        
                        if thead:
                            for row in thead.find_all('tr', recursive=False):
                                nested_rows.append(row)
                        if tbody:
                            for row in tbody.find_all('tr', recursive=False):
                                nested_rows.append(row)
                        if not thead and not tbody:
                            for row in table_tag.find_all('tr', recursive=False):
                                nested_rows.append(row)
                        
                        if not nested_rows:
                            return self._sanitize_table_html(str(table_tag))
                        
                        md_lines = []
                        for i, row in enumerate(nested_rows):
                            cells = row.find_all(['th', 'td'], recursive=False)
                            cell_texts = [escape_pipes(cell.get_text(" ", strip=True)) or ' ' for cell in cells]
                            md_lines.append('| ' + ' | '.join(cell_texts) + ' |')
                            
                            # Add separator after first row (header)
                            if i == 0:
                                md_lines.append('| ' + ' | '.join(['---'] * len(cell_texts)) + ' |')
                        
                        return '\n'.join(md_lines)
                        
                    except Exception as e:
                        # Fall back to sanitized HTML if conversion fails
                        return self._sanitize_table_html(str(table_tag))

                # Row iterator (non-recursive to avoid nested)
                def iter_rows(table_tag):
                    thead = table_tag.find('thead')
                    tbody = table_tag.find('tbody')
                    if thead:
                        for r in thead.find_all('tr', recursive=False):
                            yield r
                    if tbody:
                        for r in tbody.find_all('tr', recursive=False):
                            yield r
                    if not thead and not tbody:
                        for r in table_tag.find_all('tr', recursive=False):
                            yield r

                def cells_in_row(row):
                    return row.find_all(['th','td'], recursive=False)

                rows = list(iter_rows(el))
                if not rows:
                    return ''

                # Determine header
                header_cells = []
                body_rows = []
                if rows:
                    first_cells = cells_in_row(rows[0])
                    if any(c.name == 'th' for c in first_cells):
                        header_cells = [escape_pipes(text_without_nested(c)) or ' ' for c in first_cells]
                        body_rows = rows[1:]
                    else:
                        # Synthetic empty header with width of first row
                        header_cells = [' '] * len(first_cells)
                        body_rows = rows

                md_lines: List[str] = []
                md_lines.append('| ' + ' | '.join(header_cells) + ' |')
                md_lines.append('| ' + ' | '.join(['---'] * len(header_cells)) + ' |')

                nested_blocks: List[str] = []

                for r in body_rows:
                    cells = cells_in_row(r)
                    texts: List[str] = []
                    row_blocks: List[str] = []
                    for c in cells:
                        base_text = escape_pipes(text_without_nested(c))
                        nested_tables = c.find_all('table')
                        if nested_tables:
                            # Build label and blocks - convert nested tables to markdown
                            start_idx = nested_idx
                            for t in nested_tables:
                                # Convert nested table to markdown instead of HTML
                                nested_md = convert_nested_table_to_markdown(t)
                                row_blocks.append(f"**Table {nested_idx}:**\n\n{nested_md.strip()}\n")
                                nested_idx += 1
                            if start_idx == nested_idx - 1:
                                ref = f"[See Table {start_idx}](#table-{start_idx})"
                            else:
                                ref = f"[See Tables {start_idx}-{nested_idx-1}](#table-{start_idx})"
                            if base_text:
                                base_text = f"{base_text} ({ref})"
                            else:
                                base_text = ref
                        texts.append(base_text)
                    # Pad/truncate to header width
                    if len(texts) < len(header_cells):
                        texts += [''] * (len(header_cells) - len(texts))
                    else:
                        texts = texts[:len(header_cells)]
                    md_lines.append('| ' + ' | '.join(texts) + ' |')
                    nested_blocks.extend(row_blocks)

                out = '\n'.join(md_lines)
                if nested_blocks:
                    out += '\n\n' + '\n\n'.join(nested_blocks)
                return out + '\n'
        except Exception:
            # If anything goes wrong, fallback to parent behavior
            pass
        # Default behavior for non-nested tables
        return super().convert_table(el, text, convert_as_inline)  # type: ignore

    def _sanitize_table_html(self, html: str) -> str:
        """Strip noisy attributes and wrappers from preserved tables to reduce output bloat.
        Keeps semantic elements and colgroups/thead/tbody/tr/td/th, but removes class/style/data-*.
        Does NOT convert to Markdown; purely sanitizes HTML.
        """
        if not self.options.get("sanitize_tables_html", True):
            return html
        try:
            soup = BeautifulSoup(html, 'html.parser')
            # Remove wrappers like <div class="table-wrap"> but keep their children
            for wrap in soup.find_all(lambda t: t.name in ['div','span'] and any(getattr(c,'name',None)=='table' for c in t.children)):
                wrap.unwrap()
            # Sanitize attributes on table-related elements
            for tag in soup.find_all(['table','thead','tbody','tfoot','tr','td','th','colgroup','col']):
                allowed = {'scope','colspan','rowspan'}
                tag.attrs = {k:v for k,v in tag.attrs.items() if k in allowed}
            return str(soup)
        except Exception:
            return html

    def convert_soup(self, soup: Any) -> str:
        return super().convert_soup(soup)  # type: ignore


class HtmlConverter:
    """HTML to Markdown converter"""

    def _preprocess_html(self, html_content: str) -> str:
        """
        Preprocess HTML to clean up unwanted elements and styles.
        Mimics the Perl processing pipeline for content extraction.
        """
        # Keep from first <div id="content...>
        content_match = re.search(r'(<div id="content[^>]*>)', html_content, re.DOTALL | re.IGNORECASE)
        if content_match:
            start_pos = content_match.start(1)
            html_content = html_content[start_pos:]
        
        # Cut after first <div id="likes-and-labels-container">
        likes_match = re.search(r'(<div id="likes-and-labels-container)', html_content, re.DOTALL | re.IGNORECASE)
        if likes_match:
            end_pos = likes_match.start(1)
            html_content = html_content[:end_pos]
        
        # Remove CSS <link> tags
        html_content = re.sub(r'<link[^>]*rel="stylesheet"[^>]*/?>', '', html_content, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        
        # Remove <style>...</style> blocks
        html_content = re.sub(r'<style>.*?</style>', '', html_content, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        
        # Remove inline style attributes
        html_content = re.sub(r'style="[^"]*"', '', html_content, flags=re.MULTILINE | re.IGNORECASE)
        
        # Remove draw.io diagram containers
        html_content = re.sub(
            r'<div[^>]*id="drawio-macro-content[^"]*"[^>]*>.*?</div>\s*<script[^>]*>.*?</script>',
            '',
            html_content,
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE
        )
        
        # Remove standalone SVG elements
        html_content = re.sub(r'<svg[^>]*>.*?</svg>', '', html_content, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        
        # Remove canvas elements
        html_content = re.sub(r'<canvas[^>]*>.*?</canvas>', '', html_content, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        
        # Remove script blocks related to diagrams
        html_content = re.sub(
            r'<script[^>]*>\s*\(function\(\)\s*\{\s*function startViewer\(\).*?</script>',
            '',
            html_content,
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE
        )
        
        # Remove img elements with base64 data URIs
        html_content = re.sub(r'<img[^>]*src="data:[^"]*"[^>]*/?>\s*', '', html_content, flags=re.MULTILINE | re.IGNORECASE)
        
        # Remove draw.io evaluation text
        html_content = re.sub(r'<span[^>]*>draw\.io evaluation version</span>', '', html_content, flags=re.MULTILINE | re.IGNORECASE)
        
        # Remove diagram-related divs by class
        html_content = re.sub(
            r'<div[^>]*class="[^"]*geDiagramContainer[^"]*"[^>]*>.*?</div>',
            '',
            html_content,
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE
        )
        
        return html_content

    def convert_file(self, file_path: str, **kwargs) -> DocumentConverterResult:
        """Convert an HTML file to Markdown"""
        with open(file_path, 'rb') as file_stream:
            return self.convert_stream(file_stream, **kwargs)

    def convert_stream(self, file_stream, **kwargs) -> DocumentConverterResult:
        """Convert an HTML stream to Markdown"""
        # Read and preprocess the HTML content
        encoding = kwargs.get('encoding', 'utf-8')
        if hasattr(file_stream, 'read'):
            html_content = file_stream.read()
            if isinstance(html_content, bytes):
                html_content = html_content.decode(encoding)
        else:
            html_content = str(file_stream)
        
        # Apply preprocessing to clean up the HTML
        html_content = self._preprocess_html(html_content)
        
        # Parse the preprocessed HTML
        soup = BeautifulSoup(html_content, "html.parser")

        # Remove javascript and style blocks
        for script in soup(["script", "style"]):
            script.extract()

        # Print only the main content
        body_elm = soup.find("body")
        webpage_text = ""
        if body_elm:
            # Prefer preserving complex tables as HTML and appending nested tables by default
            webpage_text = _CustomMarkdownify(
                preserve_tables_as_html=False,
                nested_table_handling="append",  # Use append mode to extract nested tables as markdown
                sanitize_tables_html=True,
                **kwargs,
            ).convert_soup(body_elm)
        else:
            webpage_text = _CustomMarkdownify(**kwargs).convert_soup(soup)

        assert isinstance(webpage_text, str)

        # remove leading and trailing \n
        webpage_text = webpage_text.strip()

        return DocumentConverterResult(
            markdown=webpage_text,
            title=None if soup.title is None else soup.title.string,
        )

    def convert_string(self, html_content: str, **kwargs) -> DocumentConverterResult:
        """Convert an HTML string to Markdown"""
        # Apply preprocessing to clean up the HTML
        html_content = self._preprocess_html(html_content)
        
        return self.convert_stream(
            file_stream=io.BytesIO(html_content.encode("utf-8")),
            **kwargs,
        )


def _fix_markdown_tables(markdown_content: str) -> str:
    """
    Fix broken markdown tables by cleaning up cell content.
    Removes newlines and excessive whitespace within table cells.
    """
    lines = markdown_content.split('\n')
    result_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Check if this line looks like a table row (contains |)
        if '|' in line and line.strip().startswith('|') and line.strip().endswith('|'):
            # Check if next line is a separator (contains --- or similar)
            is_header = (i + 1 < len(lines) and 
                        '|' in lines[i + 1] and 
                        '---' in lines[i + 1])
            
            # Process this table row and any continuation lines
            table_row = line
            j = i + 1
            
            # Look ahead for lines that might be continuation of table cells
            while j < len(lines):
                next_line = lines[j].strip()
                
                # Stop if we hit a separator row
                if '|' in next_line and '---' in next_line:
                    break
                
                # Stop if we hit another proper table row
                if ('|' in next_line and 
                    next_line.startswith('|') and 
                    next_line.endswith('|') and
                    next_line.count('|') >= table_row.count('|')):
                    break
                
                # Stop if we hit an empty line or non-table content
                if not next_line or ('|' not in next_line):
                    break
                
                # This looks like a continuation line - merge it
                # Remove leading/trailing whitespace and merge with space
                continuation_text = next_line.strip()
                if continuation_text:
                    # Find the last cell in the current row and append the continuation
                    table_row = table_row.rstrip('|').rstrip() + ' ' + continuation_text
                    if not table_row.endswith('|'):
                        table_row += ' |'
                
                j += 1
            
            # Clean up the merged table row
            table_row = _clean_table_row(table_row)
            result_lines.append(table_row)
            
            # Skip the lines we've processed
            i = j
        else:
            result_lines.append(line)
            i += 1
    
    return '\n'.join(result_lines)


def _clean_table_row(row: str) -> str:
    """Clean up a single table row by normalizing cell content."""
    if not row.strip() or '|' not in row:
        return row
    
    # Split by | but preserve the structure
    parts = row.split('|')
    cleaned_parts = []
    
    for i, part in enumerate(parts):
        if i == 0 or i == len(parts) - 1:
            # First and last parts (before first | and after last |)
            cleaned_parts.append(part)
        else:
            # Cell content - clean up whitespace and newlines
            cleaned = part.strip()
            # Replace multiple whitespace with single space
            cleaned = re.sub(r'\s+', ' ', cleaned)
            # Remove any remaining newlines
            cleaned = cleaned.replace('\n', ' ').replace('\r', ' ')
            cleaned_parts.append(' ' + cleaned + ' ')
    
    return '|'.join(cleaned_parts)


def main(args):
    """
    DigitalOcean Functions main entry point.
    
    Expected input formats:
    1. Priority: HTML string with Content-Type: text/html
    2. Fallback: JSON with 'html' field containing HTML string:
       {"html": "<html>...</html>"}
    """
    try:
        # Handle different input formats
        html_content = None
        
        # Check if we have HTTP headers and content type
        content_type = None
        if isinstance(args, dict) and '__ow_headers' in args:
            headers = args.get('__ow_headers', {})
            content_type = headers.get('content-type', '').lower()
        
        # Priority 1: Check for text/html content type with direct HTML string
        if content_type and 'text/html' in content_type:
            # For text/html content type, treat the raw body as HTML
            if isinstance(args, dict) and '__ow_body' in args:
                # Raw body from HTTP request
                raw_body = args.get('__ow_body', '')
                if isinstance(raw_body, str) and raw_body.strip():
                    html_content = raw_body
                elif isinstance(raw_body, bytes):
                    html_content = raw_body.decode('utf-8')
            elif isinstance(args, str):
                # Direct string input
                html_content = args
        
        # Priority 2: Fallback to JSON parsing if no HTML content found yet
        if not html_content:
            if isinstance(args, dict):
                # Direct dictionary input
                if 'html' in args:
                    html_content = args['html']
                else:
                    return {
                        'statusCode': 400,
                        'body': {'error': 'Missing HTML content. Provide HTML string with Content-Type: text/html or JSON with "html" field.'}
                    }
            else:
                # String input - assume it's JSON
                try:
                    data = json.loads(args) if isinstance(args, str) else args
                    if 'html' in data:
                        html_content = data['html']
                    else:
                        return {
                            'statusCode': 400,
                            'body': {'error': 'Missing HTML content. Provide HTML string with Content-Type: text/html or JSON with "html" field.'}
                        }
                except json.JSONDecodeError as e:
                    return {
                        'statusCode': 400,
                        'body': {'error': f'Invalid JSON input: {str(e)}'}
                    }
        
        if not html_content:
            return {
                'statusCode': 400,
                'body': {'error': 'HTML content is empty'}
            }
        
        # Convert HTML to Markdown
        converter = HtmlConverter()
        result = converter.convert_string(html_content)
        
        # Normalize the content (same as MarkItDown does)
        markdown_content = "\n".join(
            [line.rstrip() for line in re.split(r"\r?\n", result.markdown)]
        )
        markdown_content = re.sub(r"\n{3,}", "\n\n", markdown_content)
        
        # Fix broken markdown tables
        markdown_content = _fix_markdown_tables(markdown_content)
        
        # Generate filename from title or use default
        filename = "converted.md"
        if result.title:
            # Sanitize title for filename
            safe_title = re.sub(r'[^\w\s-]', '', result.title.strip())
            safe_title = re.sub(r'[-\s]+', '-', safe_title)
            if safe_title:
                filename = f"{safe_title}.md"
        
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'text/markdown',
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Cache-Control': 'no-cache'
            },
            'body': markdown_content
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': {
                'error': f'Error converting HTML: {str(e)}',
                'success': False
            }
        }