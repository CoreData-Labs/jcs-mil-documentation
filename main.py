#!/usr/bin/env python3
from __future__ import annotations  # Evaluate type hints lazily so modern annotations work everywhere.

import asyncio  # Import asyncio to run Playwright's asynchronous API and background threads.
import enum  # Import enum to define a fixed set of download outcomes.
import hashlib  # Import SHA-256 hashing for duplicate PDF detection.
import logging  # Import logging so every step is written to the console at debug level.
import os  # Import operating-system helpers for atomic file replacement.
import re as regular_expressions  # Import regular expressions under a descriptive alias.
import sys  # Import sys for the console stream and the process exit code.
from collections.abc import AsyncIterator, Iterator  # Import iterator types for generator annotations.
from dataclasses import dataclass  # Import dataclass to define simple data-holding classes.
from pathlib import Path  # Import Path for reliable filesystem operations.
from typing import BinaryIO  # Import BinaryIO to annotate open binary file handles.
from urllib.parse import quote, urljoin  # Import URL joining and encoding for complete PDF links.
from playwright.async_api import APIRequestContext, Locator, Page, async_playwright  # Import Playwright's asynchronous browser API.
from playwright.async_api import Error as PlaywrightError  # Import Playwright's base error to explain a missing browser.

__version__ = "1.1.0"  # Version number reported at the start of every run.
LOGGER = logging.getLogger("jcs_pdf_scraper")  # Create the named logger used by every function in this module.
LOGGING_ALREADY_CONFIGURED = False  # Track whether console logging has been configured so handlers are not duplicated.

DEFAULT_DOWNLOAD_FOLDER_PATH = Path("PDFs")  # Store every downloaded PDF in the required PDFs directory by default.
LISTING_SECTION_URLS = (  # Define the JCS library sections that list documents.
    "https://www.jcs.mil/Library/CJCS-Instructions/",  # Instructions section.
    "https://www.jcs.mil/Library/CJCS-Manuals/",  # Manuals section.
    "https://www.jcs.mil/Library/CJCS-Notices/",  # Notices section.
    "https://www.jcs.mil/Library/CJCS-Guides/",  # Guides section.
)  # Finish the tuple of listing section addresses.
LISTING_PAGE_PARAMETER = "udt_46626_param_page"  # Query parameter the site uses to select a listing page.
MAXIMUM_PAGES_PER_SECTION = 10  # Safety limit on how many pages are read for each section.
URL_SAFE_CHARACTERS = ":/?&=#%+;,@!$'()*[]~-._"  # Preserve URL delimiters and existing percent escapes while encoding spaces and Unicode.
PDF_LINK_PATTERN = regular_expressions.compile(r"\.pdf(?:[?#]|$)", regular_expressions.IGNORECASE)  # Match hrefs that point to a PDF file.
NON_ALPHANUMERIC_PATTERN = regular_expressions.compile(r"[^a-z0-9]+")  # Match every run of characters unsuitable for a filename.
PDF_FILE_SIGNATURE = b"%PDF-"  # Every valid PDF file begins with these bytes.
CHECKSUM_CHUNK_SIZE_BYTES = 1024 * 1024  # Read files in one-megabyte chunks when hashing them.
MAXIMUM_FILENAME_STEM_LENGTH = 120  # Limit the readable part of a filename to avoid operating-system length limits.
FALLBACK_DOCUMENT_NUMBER = "jcs_document"  # Document number used when the table row has no usable number.
FALLBACK_DOCUMENT_TITLE = "untitled_document"  # Document title used when the table row has no usable title.


class ScraperError(Exception):  # Base class for every error this scraper raises deliberately.
    pass  # The base class needs no extra behaviour beyond what Exception provides.


class HttpStatusError(ScraperError):  # Raised when the server answers with an unsuccessful HTTP status.
    pass  # No extra behaviour is needed; the class name carries the meaning.


class InvalidPdfResponseError(ScraperError):  # Raised when a response body is not actually a PDF file.
    pass  # No extra behaviour is needed; the class name carries the meaning.


class DownloadOutcome(enum.Enum):  # Describe the three possible results of handling one document.
    DOWNLOADED = "downloaded"  # The PDF was new and was saved to disk.
    DUPLICATE = "duplicate"  # The PDF content (or URL) had already been handled.
    FAILED = "failed"  # The PDF could not be downloaded or saved.


@dataclass(frozen=True)  # Make the document description immutable so it can be safely passed around.
class PdfDocument:  # Hold everything needed to download and name one PDF.
    pdf_download_url: str  # Complete absolute URL of the PDF file.
    document_number: str  # Document number read from the listing table row.
    document_title: str  # Document title read from the listing table row.


@dataclass(frozen=True)  # Make the configuration immutable once it has been created.
class ScraperConfiguration:  # Hold every tunable setting in one place.
    download_folder_path: Path = DEFAULT_DOWNLOAD_FOLDER_PATH  # Folder that receives the downloaded PDFs.
    listing_section_urls: tuple[str, ...] = LISTING_SECTION_URLS  # Library sections that will be scraped.
    maximum_pages_per_section: int = MAXIMUM_PAGES_PER_SECTION  # Page limit for each section.
    run_browser_headless: bool = False  # Show the browser window unless headless mode is requested.
    wait_for_user_before_closing: bool = True  # Keep the browser open until the user presses Enter.
    maximum_download_attempts: int = 3  # Number of tries for each PDF before giving up.
    retry_backoff_seconds: float = 2.0  # Base delay that grows with each failed attempt.
    page_load_timeout_milliseconds: int = 60000  # Maximum time to wait for a listing page to load.
    listing_render_delay_milliseconds: int = 3000  # Extra time for client-side listing content to render.
    download_timeout_milliseconds: int = 60000  # Maximum time to wait for one PDF response.
    slow_motion_milliseconds: int = 500  # Delay between browser actions so the visible browser is easy to watch.
    viewport_width_pixels: int = 1440  # Browser viewport width.
    viewport_height_pixels: int = 1000  # Browser viewport height.


@dataclass  # Make the statistics mutable so counters can be updated during the run.
class DownloadStatistics:  # Count how every document ended up.
    downloaded_count: int = 0  # Number of newly saved PDFs.
    duplicate_count: int = 0  # Number of skipped duplicates.
    failed_count: int = 0  # Number of failed downloads and failed listing pages.

    def record_outcome(self, download_outcome: DownloadOutcome) -> None:  # Update the matching counter for one outcome.
        match download_outcome:  # Choose the counter by comparing against the enumeration members.
            case DownloadOutcome.DOWNLOADED:  # A new PDF was saved.
                self.downloaded_count += 1  # Increase the downloaded counter.
            case DownloadOutcome.DUPLICATE:  # A duplicate was skipped.
                self.duplicate_count += 1  # Increase the duplicate counter.
            case _:  # Anything else is treated as a failure.
                self.failed_count += 1  # Increase the failure counter.
        LOGGER.debug("[STATISTICS] Recorded %s | downloaded=%d duplicate=%d failed=%d", download_outcome.value, self.downloaded_count, self.duplicate_count, self.failed_count)  # Log the running totals.


def configure_console_logging() -> None:  # Send every log message at debug level to the console.
    global LOGGING_ALREADY_CONFIGURED  # Declare that the module-level flag is modified here.
    if LOGGING_ALREADY_CONFIGURED:  # Check whether logging was configured earlier.
        return  # Do nothing so handlers are not added twice.
    console_handler = logging.StreamHandler(sys.stdout)  # Create a handler that writes to the console.
    console_handler.setLevel(logging.DEBUG)  # Let the handler pass every message down to debug level.
    console_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(funcName)s:%(lineno)d | %(message)s"))  # Include time, level, and origin in every line.
    LOGGER.setLevel(logging.DEBUG)  # Let the logger emit every message down to debug level.
    LOGGER.addHandler(console_handler)  # Attach the console handler to the logger.
    LOGGER.propagate = False  # Prevent duplicate output through the root logger.
    LOGGING_ALREADY_CONFIGURED = True  # Remember that configuration is complete.
    LOGGER.debug("[LOGGING] Console logging configured at DEBUG level.")  # Confirm the logging setup.


def iterate_file_chunks(file_handle: BinaryIO, chunk_size_bytes: int) -> Iterator[bytes]:  # Yield a binary file piece by piece.
    while True:  # Keep reading until the end of the file is reached.
        file_chunk = file_handle.read(chunk_size_bytes)  # Read the next chunk of bytes.
        if not file_chunk:  # An empty result means the file has ended.
            break  # Stop reading.
        yield file_chunk  # Hand the chunk to the caller.


def calculate_file_sha256_checksum(file_path: Path) -> str:  # Calculate a file's SHA-256 checksum in memory-efficient chunks.
    sha256_hasher = hashlib.sha256()  # Create a new SHA-256 hash calculator.
    with file_path.open("rb") as pdf_file_handle:  # Open the existing PDF in binary read mode.
        for file_chunk in iterate_file_chunks(pdf_file_handle, CHECKSUM_CHUNK_SIZE_BYTES):  # Read the file chunk by chunk.
            sha256_hasher.update(file_chunk)  # Add the current chunk to the checksum.
    checksum_hexadecimal = sha256_hasher.hexdigest()  # Convert the finished checksum to a hexadecimal string.
    LOGGER.debug("[HASH] %s -> %s", file_path, checksum_hexadecimal)  # Log the file and its checksum.
    return checksum_hexadecimal  # Return the checksum.


def build_existing_checksum_index(download_folder_path: Path) -> dict[str, str]:  # Map checksums to filenames for PDFs already saved.
    checksum_to_filename: dict[str, str] = {}  # Create the dictionary that will be returned.
    download_folder_path.mkdir(parents=True, exist_ok=True)  # Create the PDFs folder if it does not already exist.
    assert download_folder_path.is_dir(), f"{download_folder_path} must be a directory"  # Fail early if the path is not a folder.
    for existing_pdf_path in sorted(download_folder_path.glob("*.pdf"), key=lambda candidate_path: candidate_path.name):  # Visit every saved PDF in name order.
        try:  # Handle unreadable or damaged files without stopping the scraper.
            checksum_to_filename[calculate_file_sha256_checksum(existing_pdf_path)] = existing_pdf_path.name  # Index the file by checksum.
        except OSError as operating_system_error:  # Catch filesystem errors while reading an existing PDF.
            LOGGER.debug("[WARNING] Could not hash %s: %s", existing_pdf_path, operating_system_error, exc_info=True)  # Report the file that could not be indexed.
    LOGGER.debug("[INDEX] Indexed %d existing PDF file(s).", len(checksum_to_filename))  # Report the index size.
    return checksum_to_filename  # Return the checksum index.


def sanitize_document_filename(document_number: str, document_title: str, pdf_download_url: str) -> str:  # Create a safe lowercase filename from document metadata.
    filename_stem = f"{document_number}_{document_title}".lower()  # Combine number and title, then lowercase everything.
    filename_stem = NON_ALPHANUMERIC_PATTERN.sub("_", filename_stem)  # Replace every run of unsafe characters with an underscore.
    filename_stem = filename_stem[:MAXIMUM_FILENAME_STEM_LENGTH].strip("_")  # Shorten the name and trim stray underscores.
    filename_stem = filename_stem or FALLBACK_DOCUMENT_NUMBER  # Use a fallback if nothing usable remains.
    url_suffix = hashlib.sha256(pdf_download_url.encode("utf-8")).hexdigest()[:8]  # Add a short URL-based suffix to reduce collisions.
    sanitized_filename = f"{filename_stem}_{url_suffix}.pdf"  # Assemble the final filename.
    LOGGER.debug("[FILENAME] %r + %r -> %s", document_number, document_title, sanitized_filename)  # Log how the filename was built.
    return sanitized_filename  # Return the filename.


def build_listing_page_url(listing_section_url: str, page_number: int) -> str:  # Build the address of one numbered listing page.
    listing_page_url = f"{listing_section_url}?{LISTING_PAGE_PARAMETER}={page_number}"  # Append the page-number parameter exactly once.
    LOGGER.debug("[LISTING URL] Section %s page %d -> %s", listing_section_url, page_number, listing_page_url)  # Log the built address.
    return listing_page_url  # Return the listing page address.


def build_absolute_pdf_url(listing_page_url: str, hyperlink_reference: str) -> str:  # Normalize a link's href into a complete absolute URL.
    stripped_reference = hyperlink_reference.strip()  # Remove whitespace around the href value.
    absolute_url = urljoin(listing_page_url, stripped_reference)  # Resolve relative and root-relative links against the listing page.
    encoded_url = quote(absolute_url, safe=URL_SAFE_CHARACTERS)  # Encode spaces and Unicode while keeping existing percent escapes.
    LOGGER.debug("[URL] %r -> %s", hyperlink_reference, encoded_url)  # Log the resolved URL.
    return encoded_url  # Return the complete absolute PDF URL.


async def read_document_metadata(link_locator: Locator) -> tuple[str, str]:  # Read the document number and title from the link's table row.
    document_number = FALLBACK_DOCUMENT_NUMBER  # Start with the fallback number.
    document_title = FALLBACK_DOCUMENT_TITLE  # Start with the fallback title.
    try:  # Continue with fallbacks if the page layout differs from the expected table.
        table_cell_texts = await link_locator.locator("xpath=ancestor::tr[1]").locator("td").all_inner_texts()  # Read every cell in the closest table row.
        if len(table_cell_texts) >= 2:  # The row has both a number cell and a title cell.
            document_number = table_cell_texts[0].strip() or FALLBACK_DOCUMENT_NUMBER  # Use the first cell as the number.
            document_title = table_cell_texts[1].strip() or FALLBACK_DOCUMENT_TITLE  # Use the second cell as the title.
        elif len(table_cell_texts) == 1:  # The row has only a number cell.
            document_number = table_cell_texts[0].strip() or FALLBACK_DOCUMENT_NUMBER  # Use the only cell as the number.
        else:  # The row has no cells at all.
            LOGGER.debug("[METADATA] No table cells found; using fallbacks.")  # Report that fallbacks are in use.
    except Exception as metadata_error:  # Catch any Playwright error while reading the row.
        LOGGER.debug("[WARNING] Could not read table metadata: %s", metadata_error, exc_info=True)  # Report the metadata problem.
    return document_number, document_title  # Return the metadata pair.


def attach_page_debug_listeners(page: Page) -> None:  # Log browser traffic and messages at debug level.
    observed_request_count = 0  # Count requests so log lines can be numbered.

    def log_browser_request(browser_request) -> None:  # Log one outgoing browser request.
        nonlocal observed_request_count  # Modify the counter belonging to the enclosing function.
        observed_request_count += 1  # Increase the request counter.
        LOGGER.debug("[BROWSER REQUEST #%d] %s %s", observed_request_count, browser_request.method, browser_request.url)  # Log the request.

    page.on("request", log_browser_request)  # Subscribe to every outgoing request.
    page.on("response", lambda browser_response: LOGGER.debug("[BROWSER RESPONSE] %s %s", browser_response.status, browser_response.url))  # Log every response status.
    page.on("console", lambda console_message: LOGGER.debug("[BROWSER CONSOLE] %s: %s", console_message.type, console_message.text))  # Log the page's own console output.
    page.on("pageerror", lambda page_error: LOGGER.debug("[BROWSER PAGE ERROR] %s", page_error))  # Log JavaScript errors raised by the page.
    page.on("requestfailed", lambda failed_request: LOGGER.debug("[BROWSER REQUEST FAILED] %s", failed_request.url))  # Log requests that never completed.


class JcsPdfScraper:  # Coordinate browsing, downloading, deduplicating, and saving.
    def __init__(self, scraper_configuration: ScraperConfiguration) -> None:  # Prepare the scraper's state.
        self.scraper_configuration = scraper_configuration  # Keep the configuration for later use.
        self.existing_checksum_to_filename: dict[str, str] = {}  # Checksums of every PDF saved so far.
        self.seen_pdf_urls: set[str] = set()  # URLs already handled during this run.
        self.download_statistics = DownloadStatistics()  # Counters for the final summary.

    async def iterate_pdf_documents(self, page: Page, listing_page_url: str) -> AsyncIterator[PdfDocument]:  # Yield each PDF found on one listing page.
        LOGGER.debug("[PAGE] Opening listing page: %s", listing_page_url)  # Announce the page being processed.
        await page.goto(listing_page_url, wait_until="domcontentloaded", timeout=self.scraper_configuration.page_load_timeout_milliseconds)  # Navigate to the listing page.
        await page.wait_for_timeout(self.scraper_configuration.listing_render_delay_milliseconds)  # Allow client-side content to render.
        link_locator = page.locator("a[href]")  # Locate every anchor that has an href.
        link_total = await link_locator.count()  # Count the anchors on the page.
        LOGGER.debug("[PAGE] %d anchor(s) found; filtering for PDFs.", link_total)  # Report how many anchors will be examined.
        pdf_found_count = 0  # Count the PDF links found on this page.
        for link_index in range(link_total):  # Examine each anchor in page order.
            link = link_locator.nth(link_index)  # Select the current anchor.
            hyperlink_reference = await link.get_attribute("href")  # Read the original href attribute.
            if hyperlink_reference is None or not PDF_LINK_PATTERN.search(hyperlink_reference):  # Skip anchors that are not PDF links.
                continue  # Move on to the next anchor.
            pdf_download_url = build_absolute_pdf_url(listing_page_url, hyperlink_reference)  # Resolve the complete PDF URL.
            document_number, document_title = await read_document_metadata(link)  # Read the number and title from the table row.
            pdf_found_count += 1  # Count this PDF.
            LOGGER.debug("[FOUND] %s | %s | %s", document_number, document_title, pdf_download_url)  # Log the discovered document.
            yield PdfDocument(pdf_download_url=pdf_download_url, document_number=document_number, document_title=document_title)  # Hand the document to the caller.
        LOGGER.debug("[PAGE] Found %d PDF link(s) on %s", pdf_found_count, listing_page_url)  # Report the page total.

    async def fetch_pdf_bytes(self, request_context: APIRequestContext, pdf_document: PdfDocument) -> bytes:  # Request one PDF and validate the response.
        LOGGER.debug("[DOWNLOAD] Requesting: %s", pdf_document.pdf_download_url)  # Show the URL before the request.
        response = await request_context.get(pdf_document.pdf_download_url, timeout=self.scraper_configuration.download_timeout_milliseconds)  # Send the HTTP request.
        if not response.ok:  # Check for an unsuccessful HTTP status.
            raise HttpStatusError(f"HTTP {response.status}: {pdf_document.pdf_download_url}")  # Report the exact status and URL.
        response_body = await response.body()  # Read the complete response body.
        if not response_body.startswith(PDF_FILE_SIGNATURE):  # Verify the PDF signature.
            raise InvalidPdfResponseError(f"Response is not a PDF file: {pdf_document.pdf_download_url}")  # Reject non-PDF payloads.
        LOGGER.debug("[DOWNLOAD] Received %d bytes from %s", len(response_body), pdf_document.pdf_download_url)  # Report the payload size.
        return response_body  # Return the validated PDF bytes.

    async def fetch_pdf_bytes_with_retries(self, request_context: APIRequestContext, pdf_document: PdfDocument) -> bytes:  # Retry transient failures with growing delays.
        last_error: Exception | None = None  # Remember the most recent failure.
        attempt_number = 0  # Count the attempts made so far.
        while attempt_number < self.scraper_configuration.maximum_download_attempts:  # Keep trying until the attempt limit is reached.
            attempt_number += 1  # Count the current attempt.
            try:  # Attempt the download.
                return await self.fetch_pdf_bytes(request_context, pdf_document)  # Return the bytes on success.
            except InvalidPdfResponseError:  # A non-PDF response will not improve on retry.
                raise  # Pass the error straight to the caller.
            except Exception as attempt_error:  # Any other error might be temporary.
                last_error = attempt_error  # Keep the error for the final report.
                LOGGER.debug("[RETRY] Attempt %d/%d failed: %s", attempt_number, self.scraper_configuration.maximum_download_attempts, attempt_error)  # Log the failed attempt.
                if attempt_number < self.scraper_configuration.maximum_download_attempts and last_error is not None:  # Check whether another attempt remains after a recorded failure.
                    await asyncio.sleep(self.scraper_configuration.retry_backoff_seconds * attempt_number)  # Wait longer after each failure.
                    continue  # Start the next attempt.
                break  # No attempts remain, so leave the loop.
        raise ScraperError(f"All download attempts failed: {pdf_document.pdf_download_url}") from last_error  # Report the final failure with its cause.

    def save_pdf_bytes(self, pdf_document: PdfDocument, pdf_bytes: bytes, pdf_checksum: str) -> Path:  # Write a new PDF to disk atomically.
        filename = sanitize_document_filename(pdf_document.document_number, pdf_document.document_title, pdf_document.pdf_download_url)  # Build the filename.
        destination_path = self.scraper_configuration.download_folder_path / filename  # Build the destination path.
        if destination_path.exists():  # Check whether a different file already uses this name.
            destination_path = destination_path.with_name(f"{destination_path.stem}_{pdf_checksum[:8]}.pdf")  # Add a content-based suffix to avoid overwriting.
            LOGGER.debug("[SAVE] Filename collision; using %s", destination_path.name)  # Report the collision handling.
        temporary_path = destination_path.with_suffix(".part")  # Name a temporary file for the incomplete download.
        with temporary_path.open("wb") as temporary_file_handle:  # Open the temporary file for binary writing.
            temporary_file_handle.write(pdf_bytes)  # Write the complete PDF.
        os.replace(temporary_path, destination_path)  # Atomically move the finished file into place.
        assert destination_path.is_file(), f"{destination_path} was not created"  # Confirm the file really exists.
        self.existing_checksum_to_filename[pdf_checksum] = destination_path.name  # Remember the checksum to prevent duplicates later.
        LOGGER.debug("[SAVED] %s", destination_path)  # Report the saved path.
        return destination_path  # Return where the file was saved.

    async def process_pdf_document(self, request_context: APIRequestContext, pdf_document: PdfDocument) -> DownloadOutcome:  # Download, deduplicate, and save one PDF.
        if pdf_document.pdf_download_url in self.seen_pdf_urls:  # Check whether this exact URL was already handled.
            LOGGER.debug("[SKIP] URL already handled this run: %s", pdf_document.pdf_download_url)  # Report the skipped URL.
            return DownloadOutcome.DUPLICATE  # Count it as a duplicate.
        self.seen_pdf_urls.add(pdf_document.pdf_download_url)  # Remember the URL for the rest of the run.
        try:  # Handle errors for this individual PDF.
            pdf_bytes = await self.fetch_pdf_bytes_with_retries(request_context, pdf_document)  # Download the PDF with retries.
            pdf_checksum = hashlib.sha256(pdf_bytes).hexdigest()  # Calculate the downloaded content's checksum.
            if pdf_checksum in self.existing_checksum_to_filename:  # Check whether identical content is already saved.
                LOGGER.debug("[SKIP] Duplicate content already saved as: %s", self.existing_checksum_to_filename[pdf_checksum])  # Report the existing file.
                return DownloadOutcome.DUPLICATE  # Count it as a duplicate.
            self.save_pdf_bytes(pdf_document, pdf_bytes, pdf_checksum)  # Write the new PDF to disk.
            del pdf_bytes  # Release the large byte string as soon as it is saved.
            return DownloadOutcome.DOWNLOADED  # Report a successful download.
        except (ScraperError, OSError) as download_error:  # Catch download and filesystem failures.
            LOGGER.debug("[ERROR] Could not download %s: %s", pdf_document.pdf_download_url, download_error, exc_info=True)  # Report the failure.
            return DownloadOutcome.FAILED  # Report a failed download.

    async def process_listing_page(self, page: Page, request_context: APIRequestContext, listing_page_url: str) -> int:  # Handle every PDF on one listing page and return how many were new.
        new_document_count = 0  # Count PDFs on this page that were not seen before.
        try:  # Continue to the next listing page if this one cannot be processed.
            async for pdf_document in self.iterate_pdf_documents(page, listing_page_url):  # Receive PDFs as they are discovered.
                if pdf_document.pdf_download_url not in self.seen_pdf_urls:  # Check whether this PDF has not been handled yet.
                    new_document_count += 1  # Count the new PDF.
                download_outcome = await self.process_pdf_document(request_context, pdf_document)  # Download and deduplicate the PDF.
                self.download_statistics.record_outcome(download_outcome)  # Update the counters.
        except Exception as listing_page_error:  # Catch navigation or extraction errors.
            LOGGER.debug("[ERROR] Could not process listing page %s: %s", listing_page_url, listing_page_error, exc_info=True)  # Report the page failure.
            self.download_statistics.record_outcome(DownloadOutcome.FAILED)  # Count the page-level failure.
        return new_document_count  # Report how many new PDFs this page contributed.

    async def process_listing_section(self, page: Page, request_context: APIRequestContext, listing_section_url: str) -> None:  # Walk through a section's pages until they run out.
        for page_number in range(1, self.scraper_configuration.maximum_pages_per_section + 1):  # Visit page 1, 2, 3 and so on.
            listing_page_url = build_listing_page_url(listing_section_url, page_number)  # Build this page's address.
            new_document_count = await self.process_listing_page(page, request_context, listing_page_url)  # Scrape the page.
            if new_document_count == 0:  # An empty or repeated page means the section has ended.
                LOGGER.debug("[SECTION] Page %d of %s had no new PDFs; section finished.", page_number, listing_section_url)  # Report the stop.
                break  # Move on to the next section.
        else:  # Reached only when the loop was never stopped by an empty page.
            LOGGER.debug("[SECTION] Page limit reached for %s; there may be more pages.", listing_section_url)  # Warn that the limit ended the section.

    def log_final_summary(self) -> None:  # Print the closing statistics.
        LOGGER.debug("[SUMMARY] Scraping completed.")  # Announce completion.
        LOGGER.debug("[SUMMARY] Newly downloaded PDFs: %d", self.download_statistics.downloaded_count)  # Report new downloads.
        LOGGER.debug("[SUMMARY] Duplicate PDFs skipped: %d", self.download_statistics.duplicate_count)  # Report skipped duplicates.
        LOGGER.debug("[SUMMARY] Errors: %d", self.download_statistics.failed_count)  # Report errors.
        LOGGER.debug("[SUMMARY] Download directory: %s", self.scraper_configuration.download_folder_path.resolve())  # Report the absolute folder path.

    async def run(self) -> None:  # Run the whole scraper from start to finish.
        download_folder_path = self.scraper_configuration.download_folder_path  # Shorten repeated access to the folder setting.
        self.existing_checksum_to_filename = await asyncio.to_thread(build_existing_checksum_index, download_folder_path)  # Hash existing PDFs without blocking the event loop.
        LOGGER.debug("[INFO] Existing unique PDF files indexed: %d", len(self.existing_checksum_to_filename))  # Report the index size.
        async with async_playwright() as playwright_runtime:  # Start and later stop the Playwright runtime.
            try:  # Try to start Chromium and explain clearly if that is impossible.
                browser = await playwright_runtime.chromium.launch(headless=self.scraper_configuration.run_browser_headless, slow_mo=self.scraper_configuration.slow_motion_milliseconds)  # Launch Chromium.
            except PlaywrightError as launch_error:  # Chromium is usually missing because the browser download was skipped.
                LOGGER.debug("[ERROR] Chromium could not start: %s", launch_error, exc_info=True)  # Log the technical cause.
                raise ScraperError("Chromium could not start. Install it with: python -m playwright install chromium") from launch_error  # Tell the user the fix.
            try:  # Guarantee the browser is closed even if something fails.
                viewport_settings = {"width": self.scraper_configuration.viewport_width_pixels, "height": self.scraper_configuration.viewport_height_pixels}  # Describe the viewport size.
                browser_context = await browser.new_context(viewport=viewport_settings)  # Create a browser context with that viewport.
                page = await browser_context.new_page()  # Open a browser tab for the listing pages.
                attach_page_debug_listeners(page)  # Start logging browser traffic.
                for listing_section_url in self.scraper_configuration.listing_section_urls:  # Process each library section in order.
                    await self.process_listing_section(page, browser_context.request, listing_section_url)  # Scrape every page of this section.
                self.log_final_summary()  # Print the final statistics.
                if self.scraper_configuration.wait_for_user_before_closing:  # Check whether to pause before closing.
                    await asyncio.to_thread(input, "\nPress Enter to close the browser...")  # Wait for Enter without blocking the event loop.
            finally:  # Always run the cleanup below.
                await browser.close()  # Close the browser.
                LOGGER.debug("[INFO] Browser closed.")  # Confirm the cleanup.


def main() -> int:  # Entry point that returns a process exit code.
    configure_console_logging()  # Turn on debug logging first so everything is captured.
    LOGGER.debug("[INFO] jcs_pdf_scraper %s | Python %s | platform %s", __version__, sys.version.split()[0], sys.platform)  # Log the version and environment.
    scraper_configuration = ScraperConfiguration()  # Build the configuration from its built-in defaults.
    try:  # Allow Ctrl+C to end the run cleanly.
        asyncio.run(JcsPdfScraper(scraper_configuration).run())  # Run the asynchronous scraper.
    except ScraperError as setup_error:  # A fatal, explainable problem such as a missing browser.
        LOGGER.debug("[FATAL] %s", setup_error)  # Report the problem and its fix.
        return 1  # Use a non-zero exit code so shells and schedulers notice the failure.
    except KeyboardInterrupt:  # The user interrupted the run.
        LOGGER.debug("[INFO] Interrupted by the user.")  # Report the interruption.
        return 130  # Use the conventional exit code for Ctrl+C.
    return 0  # Report success.


if __name__ == "__main__":  # Check whether this Python file is being run directly.
    sys.exit(main())  # Start the scraper and pass its exit code to the shell.
