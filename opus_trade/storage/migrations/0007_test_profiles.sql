-- Test profili yalnızca deneme içindir. Genel bakışın toplamlarına girmez,
-- herkese açık sayfada yayınlanamaz. Var olan profiller test değildir.
ALTER TABLE profiles ADD COLUMN test INTEGER NOT NULL DEFAULT 0;
