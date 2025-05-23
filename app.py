import sys
import os
import time
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote
import shutil # For checking disk space

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QTextEdit, QTreeView,
    QSplitter, QFrame, QDialog, QFormLayout, QRadioButton, QGroupBox,
    QMessageBox, QFileDialog, QTabWidget, QCheckBox, QSpinBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QDir, QTimer, QUrl
from PyQt6.QtGui import QFileSystemModel, QIcon, QColor, QPalette, QFont

# Optional Selenium Imports
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.common.exceptions import WebDriverException, TimeoutException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# --- Application Constants ---
APP_NAME = "WebClonerPy"
APP_VERSION = "0.2.1" # Updated version
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
DEFAULT_DOCS_SUBDIR = "My Cloned Websites" # For auto-path suggestion

# --- Helper Functions ---
def sanitize_filename(filename):
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)
    filename = re.sub(r'_+', '_', filename)
    filename = filename.strip('_ ')
    if not filename:
        filename = "untitled"
    max_len = 200 # Max filename length, be conservative
    return filename[:max_len]

def get_domain(url):
    try:
        return urlparse(url).netloc
    except ValueError: # Handle potential errors with malformed URLs early
        return "invalid_domain"


def get_default_save_path(url):
    domain = get_domain(url)
    sanitized_domain = sanitize_filename(domain)
    try:
        docs_path = QDir.homePath() + QDir.separator() + "Documents"
        if not os.path.exists(docs_path): # Fallback if "Documents" doesn't exist (e.g. non-standard setup)
            docs_path = QDir.currentPath()
    except Exception:
        docs_path = QDir.currentPath() # Further fallback

    default_cloner_dir = os.path.join(docs_path, DEFAULT_DOCS_SUBDIR)
    os.makedirs(default_cloner_dir, exist_ok=True) # Ensure base cloner directory exists
    return os.path.join(default_cloner_dir, sanitized_domain)


# --- Worker Thread for Cloning ---
class ClonerWorker(QThread):
    log_message = pyqtSignal(str, QColor)
    progress_updated = pyqtSignal(int)
    status_updated = pyqtSignal(int, float, float)
    file_saved = pyqtSignal(str)
    clone_finished = pyqtSignal(dict)
    page_content_downloaded = pyqtSignal(str, str)

    def __init__(self, base_url, dest_path, clone_type="recursive", headers=None,
                 use_selenium=False, selenium_timeout=30, request_delay=1, proxy_settings=None,
                 max_depth=5, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.dest_path = dest_path # This is the root for THIS clone, e.g., .../My Cloned Websites/example_com
        self.clone_type = clone_type
        self.headers = headers if headers else {"User-Agent": DEFAULT_USER_AGENT}
        
        self.use_selenium = use_selenium and SELENIUM_AVAILABLE
        self.selenium_timeout = selenium_timeout
        self.request_delay = request_delay
        self.proxy_settings = proxy_settings if proxy_settings else {}
        self.max_depth = max_depth

        self.session = requests.Session()
        self.session.headers.update(self.headers)
        if self.proxy_settings.get('http') or self.proxy_settings.get('https'):
            self.session.proxies.update(self.proxy_settings)
            self.log_message.emit(f"Using Requests proxy: {self.proxy_settings}", QColor(Qt.GlobalColor.blue))

        self.visited_urls = set()
        self.files_downloaded = 0
        self.total_size_bytes = 0
        self.start_time = 0
        self.stop_requested = False
        self.url_queue = []
        self.selenium_driver = None

    def stop(self):
        self.stop_requested = True
        self.log_message.emit("Stop request received. Finishing current tasks...", QColor(Qt.GlobalColor.yellow))
        if self.selenium_driver:
            try:
                self.selenium_driver.quit()
            except Exception as e:
                self.log_message.emit(f"Error quitting Selenium driver: {e}", QColor(Qt.GlobalColor.red))

    def _init_selenium_driver(self):
        if not self.use_selenium:
            return None
        try:
            self.log_message.emit("Initializing Selenium WebDriver (Chrome)...", QColor(Qt.GlobalColor.blue))
            chrome_options = ChromeOptions()
            chrome_options.add_argument(f"--user-agent={self.headers.get('User-Agent', DEFAULT_USER_AGENT)}")
            # chrome_options.add_argument("--headless") 
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])

            proxy_str = None
            if self.proxy_settings.get('http'):
                proxy_str = self.proxy_settings['http']
            elif self.proxy_settings.get('https'):
                proxy_str = self.proxy_settings['https']
            
            if proxy_str:
                if not proxy_str.startswith(("http://", "https://", "socks5://", "socks4://")):
                    if self.proxy_settings.get('http') and "://" not in self.proxy_settings.get('http', ''):
                         proxy_str = "http://" + self.proxy_settings.get('http')
                    elif self.proxy_settings.get('https') and "://" not in self.proxy_settings.get('https', ''):
                         proxy_str = "http://" + self.proxy_settings.get('https')
                
                chrome_options.add_argument(f'--proxy-server={proxy_str}')
                self.log_message.emit(f"Using Selenium proxy: {proxy_str}", QColor(Qt.GlobalColor.blue))

            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            driver.set_page_load_timeout(self.selenium_timeout)
            self.log_message.emit("Selenium WebDriver initialized.", QColor(Qt.GlobalColor.green))
            return driver
        except WebDriverException as e:
            self.log_message.emit(f"Failed to initialize Selenium WebDriver: {e}. Falling back to requests.", QColor(Qt.GlobalColor.red))
            self.use_selenium = False 
            return None
        except Exception as e: 
            self.log_message.emit(f"General error initializing Selenium: {e}", QColor(Qt.GlobalColor.red))
            self.use_selenium = False
            return None

    def _fetch_page_with_selenium(self, url):
        if not self.selenium_driver:
            self.selenium_driver = self._init_selenium_driver()
            if not self.selenium_driver: return None, None

        try:
            self.log_message.emit(f"Fetching (Selenium): {url}", QColor(Qt.GlobalColor.darkCyan))
            self.selenium_driver.get(url)
            time.sleep(self.request_delay + 1) # Basic wait for JS, slightly more than request_delay
            html_content = self.selenium_driver.page_source
            return html_content.encode('utf-8'), 'utf-8' # Selenium gives decoded string
        except TimeoutException:
            self.log_message.emit(f"Selenium timeout loading {url}", QColor(Qt.GlobalColor.red))
            return None, None
        except WebDriverException as e:
            self.log_message.emit(f"Selenium error fetching {url}: {e}", QColor(Qt.GlobalColor.red))
            if self.selenium_driver:
                try: self.selenium_driver.quit()
                except: pass
                self.selenium_driver = None
            return None, None

    def _fetch_page_with_requests(self, url):
        try:
            self.log_message.emit(f"Fetching (Requests): {url}", QColor(Qt.GlobalColor.darkCyan))
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
            return response.content, response.encoding, response.headers.get('Content-Type', '')
        except requests.exceptions.RequestException as e:
            self.log_message.emit(f"Failed to download (Requests) {url}: {e}", QColor(Qt.GlobalColor.red))
            return None, None, None

    def run(self):
        self.start_time = time.time()
        self.log_message.emit(f"Starting clone: {self.base_url} to {self.dest_path}", QColor(Qt.GlobalColor.blue))
        self.log_message.emit(f"Clone Type: {self.clone_type}, Max Depth: {self.max_depth}", QColor(Qt.GlobalColor.blue))

        try:
            os.makedirs(self.dest_path, exist_ok=True)
            # Initial URL uses self.dest_path as its current_save_base_path
            self.url_queue.append((self.base_url, 0, self.dest_path))
            self.visited_urls.add(self.base_url)
            
            initial_queue_size = 1

            while self.url_queue:
                if self.stop_requested: break
                
                current_url, depth, current_save_base_path_for_url = self.url_queue.pop(0)
                self.log_message.emit(f"Processing: {current_url} (depth: {depth})", QColor(Qt.GlobalColor.darkCyan))

                if self.request_delay > 0 and self.files_downloaded > 0:
                    time.sleep(self.request_delay)

                content, encoding, content_type_header = None, None, None
                use_selenium_for_this_url = self.use_selenium and depth == 0 # Example: only for initial page

                if use_selenium_for_this_url:
                    content_bytes, encoding_str = self._fetch_page_with_selenium(current_url)
                    if content_bytes:
                        content, encoding = content_bytes, encoding_str
                        content_type_header = "text/html" # Assume for Selenium main page
                    else:
                        self.log_message(f"Selenium fetch failed for {current_url}, trying Requests.", QColor(Qt.GlobalColor.yellow))
                        content, encoding, content_type_header = self._fetch_page_with_requests(current_url)
                else:
                    content, encoding, content_type_header = self._fetch_page_with_requests(current_url)

                if content is None:
                    processed_count = initial_queue_size - len(self.url_queue) # Ensure this reflects actual attempts
                    self.progress_updated.emit(int((processed_count / initial_queue_size) * 100) if initial_queue_size > 0 else 0)
                    continue
                
                is_html = False
                if content_type_header and 'text/html' in content_type_header.lower():
                    is_html = True
                elif not content_type_header: # Fallback if header is missing
                    if any(current_url.lower().endswith(ext) for ext in ['.html', '.htm', '.php']) or current_url.endswith('/'):
                        is_html = True
                    elif content: # Basic sniff for HTML tags if no other info
                        try:
                            decoded_sample = content[:1000].decode(encoding or 'utf-8', errors='ignore').lower()
                            if '<html' in decoded_sample or '<!doctype html' in decoded_sample:
                                is_html = True
                        except Exception: pass


                parsed_url = urlparse(current_url)
                path_from_url = unquote(parsed_url.path)
                path_segments = [sanitize_filename(s) for s in path_from_url.strip('/').split('/') if s]

                filename_for_current_url = ""
                local_dir_path_segments_for_current_url = []

                if path_from_url.endswith('/') or not path_segments:
                    filename_for_current_url = "index.html"
                    local_dir_path_segments_for_current_url = path_segments
                else:
                    potential_filename = path_segments[-1]
                    if '.' in potential_filename:
                        filename_for_current_url = potential_filename
                        local_dir_path_segments_for_current_url = path_segments[:-1]
                    elif is_html:
                        filename_for_current_url = potential_filename + ".html"
                        local_dir_path_segments_for_current_url = path_segments[:-1]
                    else:
                        filename_for_current_url = potential_filename 
                        local_dir_path_segments_for_current_url = path_segments[:-1]
                
                if not filename_for_current_url: # Fallback
                    filename_for_current_url = "index.html" if is_html else "resource"
                
                # local_file_dir is where the current HTML page (or resource) will be saved
                local_file_dir = current_save_base_path_for_url
                if local_dir_path_segments_for_current_url:
                    local_file_dir = os.path.join(current_save_base_path_for_url, *local_dir_path_segments_for_current_url)
                
                os.makedirs(local_file_dir, exist_ok=True)
                local_file_path = os.path.join(local_file_dir, filename_for_current_url)
                
                if self.files_downloaded % 10 == 0: # Disk space check
                    try:
                        _, _, free = shutil.disk_usage(os.path.dirname(self.dest_path)) # Check drive of dest_path root
                        if free < len(content) * 2 : 
                           self.log_message(f"Low disk space. Free: {free/1024**2:.2f}MB. Stopping.", QColor(Qt.GlobalColor.red))
                           self.stop_requested = True; break
                    except Exception as e:
                        self.log_message(f"Could not check disk space: {e}", QColor(Qt.GlobalColor.yellow))

                if is_html:
                    html_content_str = content.decode(encoding or 'utf-8', errors='replace')
                    self.page_content_downloaded.emit(current_url, html_content_str)
                    soup = BeautifulSoup(html_content_str, 'html.parser')
                    
                    tags_to_process = {
                        'a': 'href', 'link': 'href', 'iframe': 'src', 'embed': 'src', 'object': 'data',
                        'img': ['src', 'srcset', 'data-src'], 'script': 'src', 'source': 'src', 
                        'form': 'action' 
                    }
                    
                    found_new_links_on_page = 0
                    for tag_name, attr_names in tags_to_process.items():
                        if not isinstance(attr_names, list): attr_names = [attr_names]
                        
                        for attr_name in attr_names:
                            for tag in soup.find_all(tag_name, **{attr_name: True}):
                                if self.stop_requested: break
                                
                                original_link_val = tag[attr_name]
                                if not original_link_val or original_link_val.startswith(('data:', 'javascript:', 'mailto:', '#', 'tel:')):
                                    continue

                                current_link_to_process = original_link_val
                                if attr_name == 'srcset': # Handle srcset: process first valid URL
                                    links = [l.strip().split(' ')[0] for l in original_link_val.split(',')]
                                    if not links or not links[0]: continue
                                    current_link_to_process = links[0] # Process first link for now

                                absolute_link = urljoin(current_url, current_link_to_process)
                                parsed_absolute_link = urlparse(absolute_link)

                                if parsed_absolute_link.scheme not in ['http', 'https']: continue

                                is_asset_file = any(parsed_absolute_link.path.lower().endswith(ext) for ext in 
                                               ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.json',
                                                '.woff', '.woff2', '.ttf', '.otf', '.eot', 
                                                '.mp4', '.webm', '.ogg', '.mp3',
                                                '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.xml', '.txt']) or \
                                                tag_name in ['img', 'link', 'script', 'source', 'embed']


                                # Determine save path for this linked resource (asset or page)
                                link_domain = get_domain(absolute_link)
                                # base_save_path_for_link is the root directory for the link's domain
                                # (e.g., self.dest_path for same-domain, or self.dest_path/external_domain_name for others)
                                if link_domain == get_domain(self.base_url):
                                    base_save_path_for_link = self.dest_path
                                else: # External domain
                                    base_save_path_for_link = os.path.join(self.dest_path, sanitize_filename(link_domain))
                                os.makedirs(base_save_path_for_link, exist_ok=True)
                                
                                asset_path_from_url = unquote(parsed_absolute_link.path)
                                asset_path_segments = [sanitize_filename(s) for s in asset_path_from_url.strip('/').split('/') if s]
                                
                                asset_filename = ""
                                asset_local_dir_path_segments = []

                                # Determine if the linked URL points to an HTML page for filename decision
                                link_is_likely_html = any(absolute_link.lower().endswith(ext) for ext in ['.html', '.htm', '.php']) or absolute_link.endswith('/')

                                if asset_path_from_url.endswith('/') or not asset_path_segments:
                                    asset_filename = "index.html"
                                    asset_local_dir_path_segments = asset_path_segments
                                else:
                                    potential_asset_fname = asset_path_segments[-1]
                                    if '.' in potential_asset_fname:
                                        asset_filename = potential_asset_fname
                                        asset_local_dir_path_segments = asset_path_segments[:-1]
                                    elif link_is_likely_html and not is_asset_file: # Page-like link, no extension
                                        asset_filename = potential_asset_fname + ".html"
                                        asset_local_dir_path_segments = asset_path_segments[:-1]
                                    else: # Asset or other resource without extension
                                        asset_filename = potential_asset_fname
                                        asset_local_dir_path_segments = asset_path_segments[:-1]

                                if not asset_filename: asset_filename = "resource_default_name"
                                
                                asset_save_dir = base_save_path_for_link
                                if asset_local_dir_path_segments:
                                    asset_save_dir = os.path.join(base_save_path_for_link, *asset_local_dir_path_segments)
                                os.makedirs(asset_save_dir, exist_ok=True)
                                final_asset_local_path = os.path.join(asset_save_dir, asset_filename)
                                
                                new_link_value = ""
                                try:
                                    # Ensure paths are absolute and normalized for relpath
                                    abs_final_asset_local_path = os.path.abspath(final_asset_local_path)
                                    abs_local_file_dir = os.path.abspath(local_file_dir)
                                    
                                    new_link_value = os.path.relpath(abs_final_asset_local_path, start=abs_local_file_dir)
                                    new_link_value = new_link_value.replace(os.sep, '/')
                                except ValueError: # Should be rare if all under self.dest_path
                                    self.log_message(f"Path error: Could not create relative path from '{abs_local_file_dir}' to '{abs_final_asset_local_path}'. Asset link will be broken.", QColor(Qt.GlobalColor.red))
                                    new_link_value = f"#RELPATH_ERROR/{asset_filename}" # Placeholder
                                
                                if attr_name == 'srcset':
                                     # Naive update for srcset: replace only the processed part, keep other parts if any
                                     # A robust solution would parse and reconstruct srcset fully.
                                     tag[attr_name] = original_link_val.replace(current_link_to_process, new_link_value)
                                else:
                                    tag[attr_name] = new_link_value


                                if is_asset_file:
                                    if not os.path.exists(final_asset_local_path) or os.path.getsize(final_asset_local_path) == 0:
                                        asset_content, _, _ = self._fetch_page_with_requests(absolute_link) # Assets always via requests
                                        if asset_content:
                                            with open(final_asset_local_path, 'wb') as f: f.write(asset_content)
                                            self.files_downloaded += 1
                                            self.total_size_bytes += len(asset_content)
                                            self.file_saved.emit(final_asset_local_path)
                                            self.log_message.emit(f"Saved asset: {final_asset_local_path}", QColor(Qt.GlobalColor.darkGreen))
                                        else: # Failed download
                                            tag[attr_name] = f"#FAILED_DOWNLOAD_{original_link_val}"
                                elif absolute_link not in self.visited_urls and depth < self.max_depth:
                                    # Conditions for queuing a non-asset (HTML page) link:
                                    # 1. Not visited.
                                    # 2. Within max depth.
                                    # 3. If clone_type is "recursive", it must be same domain.
                                    # 4. If clone_type is "single_page", only queue if current depth is 0 (assets for main page).
                                    should_queue = False
                                    if self.clone_type == "recursive" and link_domain == get_domain(self.base_url):
                                        should_queue = True
                                    elif self.clone_type == "single_page" and depth == 0: # Assets are handled above, this is for linked pages from depth 0
                                        if link_domain == get_domain(self.base_url): # single_page only follows same-domain links from main page
                                            should_queue = True # (But assets for it are already downloaded)
                                    
                                    if should_queue:
                                        self.url_queue.append((absolute_link, depth + 1, base_save_path_for_link)) # Pass the correct base save path for the link's context
                                        self.visited_urls.add(absolute_link)
                                        found_new_links_on_page +=1
                                        initial_queue_size +=1
                                elif link_domain != get_domain(self.base_url) and not is_asset_file: # External page link, not an asset, and not queueing
                                     tag[attr_name] = absolute_link # Keep external page links absolute if not cloning them

                        if self.stop_requested: break # Break from inner attr_names loop
                    if self.stop_requested: break # Break from outer tags_to_process loop

                    html_content_str = str(soup)
                    with open(local_file_path, 'w', encoding='utf-8', errors='replace') as f:
                        f.write(html_content_str)
                    self.log_message.emit(f"Saved HTML: {local_file_path} ({found_new_links_on_page} new links queued)", QColor(Qt.GlobalColor.green))
                else: # Non-HTML content (e.g. direct CSS/JS link from queue - less common)
                    with open(local_file_path, 'wb') as f: f.write(content)
                    self.log_message(f"Saved binary/resource: {local_file_path}", QColor(Qt.GlobalColor.green))

                self.files_downloaded += 1
                self.total_size_bytes += len(content)
                self.file_saved.emit(local_file_path)
                
                processed_count = initial_queue_size - len(self.url_queue)
                self.progress_updated.emit(int((processed_count / initial_queue_size) * 100) if initial_queue_size > 0 else 100)
                
                time_elapsed = time.time() - self.start_time
                self.status_updated.emit(self.files_downloaded, self.total_size_bytes / (1024*1024), time_elapsed)
                
                if self.clone_type == "single_page" and depth == 0 and not found_new_links_on_page:
                    self.log_message("Single page clone (with its assets and direct page links if any) processing complete.", QColor(Qt.GlobalColor.blue))
                    # Don't break immediately, let any queued assets for this single page finish from the main loop if any were added by mistake here
            
        except Exception as e:
            self.log_message.emit(f"An error occurred in worker: {e}", QColor(Qt.GlobalColor.red))
            import traceback
            self.log_message.emit(traceback.format_exc(), QColor(Qt.GlobalColor.red))
        finally:
            if self.selenium_driver:
                try: self.selenium_driver.quit()
                except Exception: pass
            self.selenium_driver = None

            time_elapsed = time.time() - self.start_time
            status_msg = "Completed"
            if self.stop_requested: status_msg = "Stopped by user"
            elif self.files_downloaded == 0 and not self.url_queue: status_msg = "Failed or nothing to download"


            report = {
                "base_url": self.base_url, "destination": self.dest_path,
                "files_downloaded": self.files_downloaded, "total_size_mb": self.total_size_bytes / (1024*1024),
                "duration_seconds": time_elapsed, "status": status_msg
            }
            self.clone_finished.emit(report)
            self.log_message.emit(f"Cloning process finished. Status: {status_msg}", QColor(Qt.GlobalColor.magenta))


# --- Settings Dialog ---
class SettingsDialog(QDialog):
    def __init__(self, current_settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cloner Settings")
        self.setMinimumWidth(450)
        self.current_settings = current_settings

        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        self.headers_edit = QTextEdit()
        self.headers_edit.setPlaceholderText("User-Agent: MyCloner/1.0\nKey: Value")
        header_str = "\n".join(f"{k}: {v}" for k, v in self.current_settings.get('headers', {}).items())
        self.headers_edit.setText(header_str or DEFAULT_USER_AGENT)
        form_layout.addRow("HTTP Headers:", self.headers_edit)

        self.proxy_ip_edit = QLineEdit(self.current_settings.get('proxy_ip', ''))
        self.proxy_ip_edit.setPlaceholderText("e.g., 127.0.0.1 or socks5://127.0.0.1")
        form_layout.addRow("Proxy IP/Hostname:", self.proxy_ip_edit)
        
        self.proxy_port_edit = QLineEdit(self.current_settings.get('proxy_port', ''))
        self.proxy_port_edit.setPlaceholderText("e.g., 8080")
        form_layout.addRow("Proxy Port:", self.proxy_port_edit)

        self.selenium_timeout_spin = QSpinBox()
        self.selenium_timeout_spin.setRange(10, 300)
        self.selenium_timeout_spin.setValue(self.current_settings.get('selenium_timeout', 30))
        self.selenium_timeout_spin.setSuffix(" seconds")
        form_layout.addRow("Selenium Page Load Timeout:", self.selenium_timeout_spin)
        
        self.request_delay_spin = QSpinBox()
        self.request_delay_spin.setRange(0, 60)
        self.request_delay_spin.setValue(self.current_settings.get('request_delay', 1))
        self.request_delay_spin.setSuffix(" seconds")
        form_layout.addRow("Delay Between Requests:", self.request_delay_spin)

        self.max_depth_spin = QSpinBox()
        self.max_depth_spin.setRange(0, 50) # 0 for single page (assets only), up to 50 levels deep
        self.max_depth_spin.setValue(self.current_settings.get('max_depth', 5))
        self.max_depth_spin.setToolTip("0 = single page and its assets. >0 = recursive depth for same-domain pages.")
        form_layout.addRow("Max Recursive Depth:", self.max_depth_spin)


        layout.addLayout(form_layout)

        buttons_layout = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self.save_button)
        buttons_layout.addWidget(self.cancel_button)
        layout.addLayout(buttons_layout)

    def get_settings(self):
        headers = {}
        for line in self.headers_edit.toPlainText().splitlines():
            if ':' in line:
                key, value = line.split(":", 1)
                headers[key.strip()] = value.strip()
        if not headers.get("User-Agent"):
             headers["User-Agent"] = DEFAULT_USER_AGENT
             
        return {
            'headers': headers,
            'proxy_ip': self.proxy_ip_edit.text().strip(),
            'proxy_port': self.proxy_port_edit.text().strip(),
            'selenium_timeout': self.selenium_timeout_spin.value(),
            'request_delay': self.request_delay_spin.value(),
            'max_depth': self.max_depth_spin.value()
        }

# --- Main Application Window ---
class WebClonerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - {APP_VERSION}")
        self.setGeometry(100, 100, 1280, 800) # Slightly wider
        
        self.settings = { 
            'headers': {"User-Agent": DEFAULT_USER_AGENT},
            'proxy_ip': '', 'proxy_port': '',
            'selenium_timeout': 30, 'request_delay': 1, 'max_depth': 5,
            'use_selenium': False
        }
        self.cloner_worker = None
        self.clone_start_time = 0

        self.init_ui()
        self.update_status_timer = QTimer(self)
        self.update_status_timer.timeout.connect(self.update_runtime_status)
        
        if not SELENIUM_AVAILABLE:
            self.use_selenium_checkbox.setDisabled(True)
            self.use_selenium_checkbox.setToolTip("Selenium library not found. Install: pip install selenium webdriver-manager")
            self.log_message("Selenium library not found. Dynamic content engine (Selenium) is disabled. "
                             "For JS-heavy sites, install with: pip install selenium webdriver-manager", QColor(Qt.GlobalColor.yellow))

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        controls_frame = QFrame()
        controls_frame.setFrameShape(QFrame.Shape.StyledPanel)
        controls_layout = QVBoxLayout(controls_frame)

        input_group = QGroupBox("Input Configuration")
        input_group_layout = QFormLayout(input_group)
        
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter full URL, e.g., https://example.com")
        self.url_input.textChanged.connect(self.on_url_changed)
        input_group_layout.addRow(QLabel("Website URL:"), self.url_input)

        dest_path_layout = QHBoxLayout()
        self.dest_path_input = QLineEdit()
        self.dest_path_input.setPlaceholderText("Select or auto-generates download directory")
        self.dest_path_button = QPushButton("Browse...")
        self.dest_path_button.clicked.connect(self.browse_dest_path)
        dest_path_layout.addWidget(self.dest_path_input)
        dest_path_layout.addWidget(self.dest_path_button)
        input_group_layout.addRow(QLabel("Destination Path:"), dest_path_layout)
        
        controls_layout.addWidget(input_group)

        options_group = QGroupBox("Clone Options")
        options_main_layout = QVBoxLayout(options_group)
        
        clone_type_layout = QHBoxLayout()
        self.single_page_radio = QRadioButton("Single Page (incl. assets, 0-depth links)")
        self.single_page_radio.setToolTip("Clones the specified URL, its assets, and links found on that page (if same domain and depth 0).")
        self.recursive_radio = QRadioButton("Recursive Deep Clone (same domain)")
        self.recursive_radio.setToolTip("Clones the site by following links within the same domain, up to the Max Recursive Depth.")
        self.recursive_radio.setChecked(True)
        clone_type_layout.addWidget(self.single_page_radio)
        clone_type_layout.addWidget(self.recursive_radio)
        clone_type_layout.addStretch()
        options_main_layout.addLayout(clone_type_layout)

        self.use_selenium_checkbox = QCheckBox("Use Dynamic Content Engine (Selenium - for JS sites, slower)")
        self.use_selenium_checkbox.setChecked(self.settings.get('use_selenium', False))
        self.use_selenium_checkbox.toggled.connect(lambda checked: self.settings.update({'use_selenium': checked}))
        options_main_layout.addWidget(self.use_selenium_checkbox)
        
        controls_layout.addWidget(options_group)

        action_buttons_layout = QHBoxLayout()
        # Attempt to use standard icons with text fallbacks
        try:
            self.start_button = QPushButton(QIcon.fromTheme("media-playback-start", QIcon("icons/start.png")), " Start Cloning") # icon path relative
        except: self.start_button = QPushButton("Start Cloning")
        self.start_button.clicked.connect(self.start_cloning)

        try:
            self.stop_button = QPushButton(QIcon.fromTheme("media-playback-stop", QIcon("icons/stop.png")), " Stop Cloning")
        except: self.stop_button = QPushButton("Stop Cloning")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_cloning)
        
        try:
            self.settings_button = QPushButton(QIcon.fromTheme("preferences-system", QIcon("icons/settings.png")), " Settings")
        except: self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.open_settings)
        
        action_buttons_layout.addWidget(self.start_button)
        action_buttons_layout.addWidget(self.stop_button)
        action_buttons_layout.addStretch()
        action_buttons_layout.addWidget(self.settings_button)
        controls_layout.addLayout(action_buttons_layout)
        
        main_layout.addWidget(controls_frame)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        left_panel_widget = QWidget()
        left_panel_layout = QVBoxLayout(left_panel_widget)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setValue(0)
        left_panel_layout.addWidget(QLabel("Overall Progress:"))
        left_panel_layout.addWidget(self.progress_bar)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Courier New", 9))
        left_panel_layout.addWidget(QLabel("Activity Logs:"))
        left_panel_layout.addWidget(self.log_output)
        main_splitter.addWidget(left_panel_widget)

        right_tabs = QTabWidget()
        self.preview_tab = QWidget()
        preview_layout = QVBoxLayout(self.preview_tab)
        self.page_preview_source = QTextEdit() 
        self.page_preview_source.setReadOnly(True)
        self.page_preview_source.setPlaceholderText("HTML source of the currently processed page will appear here...")
        self.page_preview_source.setFont(QFont("Courier New", 9))
        preview_layout.addWidget(QLabel("Live Page Preview (HTML Source):"))
        preview_layout.addWidget(self.page_preview_source)
        right_tabs.addTab(self.preview_tab, "HTML Preview")

        self.dir_tree_tab = QWidget()
        dir_tree_layout = QVBoxLayout(self.dir_tree_tab)
        self.dir_model = QFileSystemModel()
        # self.dir_model.setFilter(QDir.Filter.NoDotAndDotDot | QDir.Filter.AllDirs | QDir.Filter.Files) # More refined filter
        self.dir_model.setRootPath(QDir.currentPath()) # Will be updated on clone start
        self.dir_tree_view = QTreeView()
        self.dir_tree_view.setModel(self.dir_model)
        self.dir_tree_view.setAnimated(False)
        self.dir_tree_view.setIndentation(15)
        self.dir_tree_view.setSortingEnabled(True)
        self.dir_tree_view.header().setStretchLastSection(False)
        self.dir_tree_view.setColumnWidth(0, 300) # Path column wider
        dir_tree_layout.addWidget(QLabel("Cloned Files Directory Tree:"))
        dir_tree_layout.addWidget(self.dir_tree_view)
        right_tabs.addTab(self.dir_tree_tab, "Directory View")

        self.stats_tab = QWidget()
        stats_layout = QFormLayout(self.stats_tab)
        self.files_label = QLabel("0")
        self.size_label = QLabel("0.00 MB")
        self.time_label = QLabel("00:00:00")
        self.avg_speed_label = QLabel("0.00 MB/s")
        self.status_label = QLabel("Idle") # Overall status
        stats_layout.addRow("Current Status:", self.status_label)
        stats_layout.addRow("Files Downloaded:", self.files_label)
        stats_layout.addRow("Total Size:", self.size_label)
        stats_layout.addRow("Time Elapsed:", self.time_label)
        stats_layout.addRow("Average Speed:", self.avg_speed_label)
        right_tabs.addTab(self.stats_tab, "Clone Statistics")
        
        main_splitter.addWidget(right_tabs)
        main_splitter.setSizes([450, 750]) 
        main_layout.addWidget(main_splitter)
        
        self.log_message(f"{APP_NAME} {APP_VERSION} started. Ready to clone.", QColor(Qt.GlobalColor.darkGray))

    def on_url_changed(self, text_url):
        # Suggest path only if user hasn't manually set one or it's empty
        if not self.dest_path_input.property("user_edited") and \
           (text_url.startswith("http://") or text_url.startswith("https://")):
            try:
                parsed = urlparse(text_url)
                if parsed.netloc: # Basic validation that it has a domain part
                    suggested_path = get_default_save_path(text_url)
                    self.dest_path_input.setText(suggested_path)
                    self.dest_path_input.setProperty("user_edited", False) # Reset flag
            except Exception: # Malformed URL, do nothing
                pass
    
    def browse_dest_path(self):
        current_path = self.dest_path_input.text() or QDir.homePath()
        path = QFileDialog.getExistingDirectory(self, "Select Destination Directory", current_path)
        if path:
            self.dest_path_input.setText(path)
            self.dest_path_input.setProperty("user_edited", True) # User has now explicitly set the path

    def format_time(self, seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02}:{m:02}:{s:02}"

    def update_runtime_status(self):
        if self.cloner_worker and self.cloner_worker.isRunning():
            elapsed_time = time.time() - self.clone_start_time
            self.time_label.setText(self.format_time(elapsed_time))
            self.status_label.setText("Cloning active...")

    def log_message(self, message, color=None):
        if color: self.log_output.setTextColor(color)
        else: self.log_output.setTextColor(self.palette().color(QPalette.ColorRole.Text))
        self.log_output.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        self.log_output.ensureCursorVisible() # Scroll to bottom

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def update_status(self, files, size_mb, time_elapsed_worker): # From worker
        self.files_label.setText(str(files))
        self.size_label.setText(f"{size_mb:.2f} MB")
        # self.time_label is updated by the main window's timer for overall time
        if time_elapsed_worker > 0.1:
            avg_speed = size_mb / time_elapsed_worker
            self.avg_speed_label.setText(f"{avg_speed:.2f} MB/s")
        else:
            self.avg_speed_label.setText("N/A")


    def update_page_preview(self, url, html_content):
        self.page_preview_source.setPlainText(f"-- Preview for {url} --\n\n{html_content}")
        # self.log_message(f"Preview updated for {url}", QColor(Qt.GlobalColor.darkMagenta))

    def update_directory_view(self, root_path):
        if not os.path.exists(root_path):
            self.log_message(f"Directory view path does not exist: {root_path}", QColor(Qt.GlobalColor.yellow))
            return
        index = self.dir_model.setRootPath(root_path)
        self.dir_tree_view.setRootIndex(index)
        self.dir_tree_view.scrollTo(index) # Ensure root is visible
        # self.log_message(f"Directory view monitoring: {root_path}", QColor(Qt.GlobalColor.darkMagenta))


    def clone_finished_report(self, report):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.settings_button.setEnabled(True)
        self.use_selenium_checkbox.setEnabled(SELENIUM_AVAILABLE)
        self.progress_bar.setValue(100)
        self.update_status_timer.stop()
        self.status_label.setText(report['status'])
        self.time_label.setText(self.format_time(report['duration_seconds'])) # Final time from worker

        report_msg_title = f"Cloning {report['status']}"
        report_msg_details = (
            f"Base URL: {report['base_url']}\n"
            f"Destination: {report['destination']}\n"
            f"Status: {report['status']}\n"
            f"Files Downloaded: {report['files_downloaded']}\n"
            f"Total Size: {report['total_size_mb']:.2f} MB\n"
            f"Duration: {self.format_time(report['duration_seconds'])}"
        )
        full_report_msg = f"Clone Report:\n--------------------------\n{report_msg_details}\n--------------------------"
        self.log_message(full_report_msg, QColor(Qt.GlobalColor.darkGreen))
        QMessageBox.information(self, report_msg_title, report_msg_details)

        self.log_message("---------------------------------------------------------", QColor(Qt.GlobalColor.blue))
        self.log_message("RECOMMENDATION FOR VIEWING THE CLONED SITE:", QColor(Qt.GlobalColor.blue))
        self.log_message("For best results and to avoid browser security errors (like CORS for 'file:///' links), "
                         "view the cloned site using a local HTTP server.", QColor(Qt.GlobalColor.blue))
        self.log_message(f"1. Open a terminal or command prompt.", QColor(Qt.GlobalColor.blue))
        self.log_message(f"2. Navigate into the cloned site's main folder: cd \"{report['destination']}\"", QColor(Qt.GlobalColor.blue))
        self.log_message(f"3. Run a simple server. If Python is installed: python -m http.server", QColor(Qt.GlobalColor.blue))
        self.log_message(f"4. Open your browser to: http://localhost:8000 (or the address shown in terminal).", QColor(Qt.GlobalColor.blue))
        self.log_message("---------------------------------------------------------", QColor(Qt.GlobalColor.blue))


    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings.update(dialog.get_settings())
            self.log_message("Settings updated.", QColor(Qt.GlobalColor.blue))

    def start_cloning(self):
        base_url = self.url_input.text().strip()
        dest_path = self.dest_path_input.text().strip()

        if not (base_url.startswith("http://") or base_url.startswith("https://")) or "." not in urlparse(base_url).netloc:
            QMessageBox.warning(self, "Invalid URL", "Please enter a valid URL (e.g., http://example.com).")
            return
        
        if not dest_path:
            self.log_message("Destination path is empty. Attempting to auto-generate.", QColor(Qt.GlobalColor.yellow))
            dest_path = get_default_save_path(base_url)
            self.dest_path_input.setText(dest_path)
            self.log_message(f"Using auto-generated destination: {dest_path}", QColor(Qt.GlobalColor.blue))
        
        try:
            os.makedirs(dest_path, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "Path Creation Error", f"Could not create or access destination directory: {dest_path}\nError: {e}")
            return
        if not os.access(dest_path, os.W_OK):
            QMessageBox.critical(self, "Path Permission Error", f"Cannot write to destination directory: {dest_path}\nPlease check permissions.")
            return


        # self.log_output.clear() # Keep old logs for context, or clear per clone? User preference.
        self.page_preview_source.clear()
        self.progress_bar.setValue(0)
        self.files_label.setText("0")
        self.size_label.setText("0.00 MB")
        self.time_label.setText("00:00:00")
        self.avg_speed_label.setText("N/A")
        self.status_label.setText("Initializing...")


        clone_type = "recursive" if self.recursive_radio.isChecked() else "single_page"
        
        self.update_directory_view(dest_path) 

        proxy_config = {}
        if self.settings['proxy_ip'] and self.settings['proxy_port']:
            # Selenium takes proxy as "host:port" or "scheme://host:port"
            # Requests takes {'http': 'http://host:port', 'https': 'http://host:port'}
            # or {'http': 'socks5://host:port', 'https': 'socks5://host:port'}
            proxy_ip = self.settings['proxy_ip']
            proxy_port = self.settings['proxy_port']
            
            # For requests, always prepend http:// for http/https proxies if no scheme given
            # SOCKS proxies need explicit scheme for requests. Selenium infers sometimes.
            if "://" in proxy_ip: # User provided scheme e.g. socks5://
                 proxy_url_for_requests = f"{proxy_ip}:{proxy_port}"
                 proxy_url_for_selenium = f"{proxy_ip}:{proxy_port}" # Selenium also takes scheme
            else: # No scheme, assume http for requests, direct for selenium
                 proxy_url_for_requests = f"http://{proxy_ip}:{proxy_port}"
                 proxy_url_for_selenium = f"{proxy_ip}:{proxy_port}" # Selenium often works without scheme for http/s

            proxy_config['http'] = proxy_url_for_requests
            proxy_config['https'] = proxy_url_for_requests # Requests usually uses http:// for https proxies too
                                                        # unless it's a specific https proxy server.
            # For Selenium, it's set in _init_selenium_driver using just one proxy string.
            # So we pass the raw parts, and selenium worker will format.
            # The self.proxy_settings in worker will take what we put in proxy_config here.

        self.cloner_worker = ClonerWorker(
            base_url, dest_path, clone_type, 
            headers=self.settings['headers'],
            use_selenium=self.settings['use_selenium'],
            selenium_timeout=self.settings['selenium_timeout'],
            request_delay=self.settings['request_delay'],
            proxy_settings=proxy_config, # Pass the dict for requests, selenium worker will adapt
            max_depth=self.settings['max_depth']
        )
        self.cloner_worker.log_message.connect(self.log_message)
        self.cloner_worker.progress_updated.connect(self.update_progress)
        self.cloner_worker.status_updated.connect(self.update_status)
        self.cloner_worker.page_content_downloaded.connect(self.update_page_preview)
        # QFileSystemModel auto-updates, direct signal for file_saved not strictly needed for tree view
        # self.cloner_worker.file_saved.connect(lambda path: self.log_message(f"File saved: {path}", QColor(Qt.GlobalColor.gray)))
        self.cloner_worker.clone_finished.connect(self.clone_finished_report)
        
        self.clone_start_time = time.time()
        self.update_status_timer.start(1000) # Update UI time every second
        self.cloner_worker.start()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.settings_button.setEnabled(False)
        if self.settings['use_selenium'] and SELENIUM_AVAILABLE: # Check SELENIUM_AVAILABLE again
            self.use_selenium_checkbox.setEnabled(False)


    def stop_cloning(self):
        if self.cloner_worker and self.cloner_worker.isRunning():
            self.log_message("User requested stop. Attempting to halt worker...", QColor(Qt.GlobalColor.yellow))
            self.status_label.setText("Stopping...")
            self.cloner_worker.stop()
            self.stop_button.setEnabled(False) # Prevent multiple stop clicks
            # Finished signal will re-enable start button etc.

    def closeEvent(self, event):
        if self.cloner_worker and self.cloner_worker.isRunning():
            reply = QMessageBox.question(self, 'Cloning in Progress',
                                         "A cloning task is currently active. Are you sure you want to quit?\n"
                                         "This will attempt to stop the current task.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.log_message("Application quit requested during active clone. Stopping worker...", QColor(Qt.GlobalColor.yellow))
                self.stop_cloning()
                if not self.cloner_worker.wait(7000): # Wait up to 7s for graceful thread termination
                    self.log_message("Worker thread did not terminate gracefully within timeout.", QColor(Qt.GlobalColor.red))
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle("Fusion") # Apply Fusion style for better cross-platform consistency

    # Create dummy icon files if they don't exist for testing purposes
    # In a real deployment, these should be proper icons.
    os.makedirs("icons", exist_ok=True)
    for icon_name in ["start.png", "stop.png", "settings.png"]:
        icon_path = os.path.join("icons", icon_name)
        if not os.path.exists(icon_path):
            try: # Create a tiny placeholder if QPixmap is available early, or skip
                from PyQt6.QtGui import QPixmap
                pixmap = QPixmap(16,16)
                pixmap.fill(Qt.GlobalColor.transparent) # or some color
                pixmap.save(icon_path)
            except:
                pass # Cannot create placeholder icon

    main_win = WebClonerApp()
    main_win.show()
    sys.exit(app.exec())