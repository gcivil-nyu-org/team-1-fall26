
from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def chat_home(request):
    return render(request, "chat/chat_home.html")

