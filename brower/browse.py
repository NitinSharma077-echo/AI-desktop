from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain.chat_models import ChatOpenAI,ChatOllama
from langchain.schema import BaseOutputParser,structuredOutputParser
import os

llm = ChatOpenAI(temperature=0, openai_api_key=os.environ["OPENAI_API_KEY"])
