import csv
import time
from datetime import date, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By

def extract_baps_messages_selenium(url, start_year):
    print(f"\nFetching data from: {url}")
    
    # 1. Setup Chrome options to run invisibly (headless)
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument('--log-level=3') # Suppress warnings
    
    # 2. Automatically download and setup the correct Chrome driver
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    try:
        driver.get(url)
        print("⏳ Waiting 5 seconds for the gallery and JavaScript to fully load...")
        time.sleep(5) # This is the crucial pause!
        
        messages = []
        
        # 3. Method A: Search for common caption classes in the rendered HTML
        caption_elements = driver.find_elements(By.CSS_SELECTOR, ".caption, .desc, .text, [data-caption], .lg-sub-html")
        for el in caption_elements:
            # Try to get visible text, or check data-caption attribute
            text = el.text.strip()
            if not text:
                text = el.get_attribute("data-caption")
                
            if text and len(text) > 15 and "BAPS" not in text and text not in messages:
                messages.append(text.strip())
                
        # Method B: If captions aren't in divs, check image alt tags
        if len(messages) < 10:
            images = driver.find_elements(By.TAG_NAME, "img")
            for img in images:
                alt_text = img.get_attribute("alt")
                if alt_text and len(alt_text.strip()) > 15 and "BAPS" not in alt_text and alt_text.strip() not in messages:
                    messages.append(alt_text.strip())

        if not messages:
            print("❌ Could not find the messages even after waiting for the page to load.")
            return

        print(f"✅ Found {len(messages)} potential messages.")
        
        # 4. Handle chronological ordering
        # If there are exactly 30 or more, take the top 30 and reverse them (Jan 14 -> Dec 16 to Dec 16 -> Jan 14)
        if len(messages) >= 30:
            messages = messages[:30]
            messages.reverse()
            
        # 5. Format and Save to CSV
        start_date = date(start_year, 12, 16)
        csv_filename = f"brahmavidya_{start_year}_{start_year+1}.csv"
        
        with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['date', 'message', 'theme'])
            
            for i in range(len(messages)):
                current_date = start_date + timedelta(days=i)
                date_str = current_date.strftime("%Y-%m-%d")
                writer.writerow([date_str, messages[i], ""])
                
        print(f"🎉 Data successfully formatted and saved to '{csv_filename}'")
        
    finally:
        # Ensure the hidden browser closes even if there is an error
        driver.quit()

# --- EXECUTION ---
urls_to_scrape = [
    {
        "year": 2024, 
        "url": "https://www.baps.org/News/2024/Brahmavidya---Dhanurmas-Messages-from-HH-Mahant-Swami-Maharaj-2024-25-27698.aspx"
    }
]

for item in urls_to_scrape:
    extract_baps_messages_selenium(item["url"], item["year"])