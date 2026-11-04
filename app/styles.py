"""Film stilleri: geçişler, renk, tempo."""

STYLES = {
    "sade": {
        "label": "Sade",
        "photo_sec": 3.5,
        "transition": 0.5,
        "transitions": ["fade"],
        "beat_stride": 2,
        "grade": "eq=saturation=1.05:contrast=1.03",
    },
    "sinematik": {
        "label": "Sinematik",
        "photo_sec": 4.0,
        "transition": 0.8,
        "transitions": ["fade", "fade", "smoothleft", "fade", "smoothright"],
        "beat_stride": 2,
        # Hafif S eğrisi + sıcak orta tonlar / serin gölgeler + kenar karartma + ince gren
        "grade": ("curves=master='0/0 0.25/0.21 0.75/0.79 1/1',"
                  "colorbalance=rs=-0.04:gs=-0.01:bs=0.05:rm=0.04:bm=-0.03:rh=0.05:bh=-0.04,"
                  "eq=saturation=0.95,vignette=angle=PI/5,noise=alls=3:allf=t"),
    },
    "enerjik": {
        "label": "Enerjik",
        "photo_sec": 2.5,
        "transition": 0.35,
        "transitions": ["fade", "slideleft", "circleopen", "fade", "slideright", "wipeleft", "radial"],
        "beat_stride": 1,
        "grade": "eq=saturation=1.18:contrast=1.07:brightness=0.01",
    },
}

LETTERBOX = ("drawbox=x=0:y=0:w=iw:h=138:color=black:t=fill,"
             "drawbox=x=0:y=ih-138:w=iw:h=138:color=black:t=fill")


def get(name):
    return STYLES.get(name or "sinematik", STYLES["sinematik"])
