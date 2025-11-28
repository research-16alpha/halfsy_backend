from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException
from pymongo import MongoClient
from bson import ObjectId
from typing import List, Optional
from pydantic import BaseModel, EmailStr
from datetime import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import math

load_dotenv()

app = FastAPI(title="Halfsy API")

# CORS middleware to allow frontend to access the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.halfsyshop.com",
    "https://www.halfsy.shop",
    "https://halfsy.shop"],  # In production, replace with your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# MongoDB connection
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME")
COLLECTION_NAME = os.getenv("COLLECTION_NAME")

# Outlook email configuration
OUTLOOK_USER = os.getenv("OUTLOOK_USER")
OUTLOOK_PASSWORD = os.getenv("OUTLOOK_PASSWORD")

try:
    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]
    products_collection = db[COLLECTION_NAME]
    messages_collection = db["messages"]
    # Test connection
    client.admin.command('ping')
    print("✅ Connected to MongoDB")
except Exception as e:
    print(f"❌ MongoDB connection error: {e}")
    products_collection = None
    messages_collection = None


# Pydantic models
class ContactForm(BaseModel):
    email: EmailStr
    message: str


@app.get("/")
def read_root():
    return {"message": "Halfsy API is running"}


@app.get("/api/products/top-deals")
def get_top_deals(limit: int = 4):
    """
    Fetch top deals from MongoDB (products with highest discounts or first products)
    - limit: Number of top deals to return (default: 4)
    """
    if products_collection is None:
        raise HTTPException(status_code=500, detail="Database connection not available")
    
    try:
        # Try to get products with discounts first
        # Filter products that have disc_pct field
        products_with_discount = list(
            products_collection.find(
                {"disc_pct": {"$exists": True, "$ne": None, "$ne": ""}},
                {"_id": 0}
            ).limit(limit * 2)  # Get more to sort
        )
        
        # Sort by discount percentage (extract number from string like "-50%")
        def extract_discount(product):
            disc_pct = product.get("disc_pct", "-0%")
            try:
                # Remove "-" and "%" and convert to int
                num_str = disc_pct.replace("-", "").replace("%", "")
                return int(num_str) if num_str.isdigit() else 0
            except:
                return 0
        
        if products_with_discount:
            products_with_discount.sort(key=extract_discount, reverse=True)
            top_deals = products_with_discount[:limit]
        else:
            top_deals = []
        
        # If we don't have enough, fill with regular products
        if len(top_deals) < limit:
            remaining = limit - len(top_deals)
            # Get products we haven't already included
            existing_ids = [p.get("product_link") for p in top_deals if p.get("product_link")]
            additional = list(
                products_collection.find(
                    {"product_link": {"$nin": existing_ids}} if existing_ids else {},
                    {"_id": 0}
                ).limit(remaining)
            )
            top_deals.extend(additional)
        
        return top_deals[:limit]
    except Exception as e:
        # Fallback: just return first 4 products
        try:
            return list(products_collection.find({}, {"_id": 0}).limit(limit))
        except Exception as fallback_error:
            raise HTTPException(status_code=500, detail=f"Error fetching top deals: {str(e)}")

@app.get("/api/products")
def get_products(
    page: int = 1,
    limit: int = 100,
    brand: str | None = None,
    min_discount: str | None = None,
    sort_by: str | None = None,
    sort_order: str = "asc"
):
    if page < 1:
        raise HTTPException(400, "Page must be >= 1")

    skip = (page - 1) * limit

    # Base Mongo query (MATCH)
    query = {}
    if brand:
        query["brand_name"] = brand

    if min_discount not in (None, ""):
        try:
            query["discount"] = {"$gte": int(min_discount)}
        except:
            pass

    PRIORITY_BRANDS = ["Brunello Cucinelli", "Brioni", "Zimmermann"]

    # Build aggregation pipeline
    pipeline = []

    # MATCH stage
    pipeline.append({"$match": query})

    # Add brand_priority field ONLY when brand filter is not applied
    if not brand:
        pipeline.append({
            "$addFields": {
                "brand_priority": {
                    "$cond": [
                        {"$in": ["$brand_name", PRIORITY_BRANDS]},
                        0,   # priority bucket
                        1    # normal bucket
                    ]
                }
            }
        })
    else:
        # If brand filter present, set all to same bucket
        pipeline.append({"$addFields": {"brand_priority": 1}})

    # SORT stage
    sort_stage = {
        "brand_priority": 1,   # priority brands first
        # "scraped_at": -1       # newest first ALWAYS
    }

    if sort_by:
        order = 1 if sort_order == "asc" else -1
        sort_stage[sort_by] = order

    pipeline.append({"$sort": sort_stage})

    # PAGINATION
    pipeline.append({"$skip": skip})
    pipeline.append({"$limit": limit})

    pipeline.append({
    "$project": {
        "_id": 0
    }
    })

    # Get total count separately
    total_products = products_collection.count_documents(query)

    # Get documents
    products = list(products_collection.aggregate(pipeline))

    return {
        "products": products,
        "total_products": total_products,
        "limit": limit,
        "page": page,
        "total_pages": math.ceil(total_products / limit),
        "has_more": (skip + limit) < total_products
    }



def send_outlook_notification(email: str, message: str):
    """
    Send email notification to Outlook about new contact form submission
    """
    if not OUTLOOK_USER or not OUTLOOK_PASSWORD:
        print("⚠️ Outlook credentials not configured, skipping email notification")
        return False
    
    try:
        # Create message
        msg = MIMEMultipart()
        msg['From'] = OUTLOOK_USER
        msg['To'] = OUTLOOK_USER
        msg['Subject'] = "New Contact Form Submission - Halfsy.shop"
        
        # Email body
        body = f"""
        You have received a new contact form submission from Halfsy.shop:
        
        From: {email}
        Message:
        {message}
        
        ---
        This is an automated notification from Halfsy.shop contact form.
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        # Send email using Outlook SMTP
        server = smtplib.SMTP('smtp-mail.outlook.com', 587)
        server.starttls()
        server.login(OUTLOOK_USER, OUTLOOK_PASSWORD)
        text = msg.as_string()
        server.sendmail(OUTLOOK_USER, OUTLOOK_USER, text)
        server.quit()
        
        print("✅ Email notification sent successfully")
        return True
    except Exception as e:
        print(f"❌ Error sending email: {str(e)}")
        return False


@app.post("/api/contact")
def submit_contact_form(contact: ContactForm):
    """
    Handle contact form submission
    - Store in MongoDB
    - Send email notification to Outlook
    """
    if messages_collection is None:
        raise HTTPException(status_code=500, detail="Database connection not available")
    
    try:
        # Prepare document for MongoDB
        contact_doc = {
            "email": contact.email,
            "message": contact.message,
            "timestamp": datetime.now(ZoneInfo("UTC"))
        } 
        
        # Store in MongoDB
        result = messages_collection.insert_one(contact_doc)
        
        # Send email notification (non-blocking - don't fail if email fails)
        send_outlook_notification(contact.email, contact.message)
        
        return {
            "success": True,
            "message": "Thank you for contacting us! We'll get back to you soon.",
            "id": str(result.inserted_id)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing contact form: {str(e)}")


@app.get("/api/brands")
def get_brands():
    """
    Fetch all brands from MongoDB
    """
    if products_collection is None:
        raise HTTPException(status_code=500, detail="Database connection not available")
    
    try:
        brands = list(products_collection.distinct("brand_name"))
        return {"brands": brands}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching brands: {str(e)}")