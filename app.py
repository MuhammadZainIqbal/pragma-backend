import gradio as gr
from app.main import app as fastapi_app
from fastapi.middleware.cors import CORSMiddleware

with gr.Blocks(title="PRAGMA API Gateway") as demo:
    gr.Markdown("# PRAGMA Secure API Gateway")
    gr.Markdown("This Gradio Space serves as the background API gateway for the PRAGMA Autonomous Code Review platform.")

# Access the underlying FastAPI app instance
app = demo.app

# Mount CORS configurations onto this active instance
origins = [
    "http://localhost:5173",
    "https://pragma-frontend.vercel.app", 
    "https://pragma.usbro.dev",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inject all of original FastAPI app routes into the Gradio app
app.include_router(fastapi_app.router)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
